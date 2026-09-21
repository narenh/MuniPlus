/**
 * A caching proxy in front of 511's StopMonitoring endpoint.
 *
 * The app used to call 511 directly, once per stop, straight from view timers:
 * no cache, no ceiling, and an API key compiled into the binary. A single
 * always-on dashboard burned 300 requests an hour of a 500/hr allowance, and
 * because a failed fetch never advanced the client's "last updated" clock, an
 * app that got throttled retried at full speed — hardest exactly when it was
 * already over the line.
 *
 * This sits in the middle and does three things: holds the key, collapses
 * repeat requests for the same stop into one upstream call, and refuses to
 * exceed a fixed hourly budget no matter who is asking or how often.
 *
 * This path deliberately never parses a large payload. Slicing 511's
 * whole-agency SIRI feed would be the obvious way to serve every stop from one
 * upstream call, but that feed is 20.8 MB and costs ~40 ms of CPU to parse,
 * against a 10 ms per-invocation ceiling on the Workers free plan — a ceiling
 * that applies to Cron Triggers too. 511 also ignores every parameter that
 * would trim the feed and rejects comma-separated stop codes, so there is no
 * smaller version of it to ask for. Requests here are passed through byte for
 * byte instead, which keeps CPU near 1 ms and leaves the app's decoder alone.
 *
 * What that reasoning missed is that SIRI is not the only way to ask. 511
 * publishes the same predictions as GTFS-Realtime TripUpdates, and measured on
 * the same afternoon that feed is 667 KB and scans in ~4 ms — 32x smaller, for
 * the identical 3,073 stops and 51 routes, with stop ids identical to SIRI's
 * StopPointRef. The /gtfs routes below do what this header says is impossible,
 * because against that feed it isn't. They are additive and unproven; the SIRI
 * path above is untouched and still the one the app talks to.
 */

const UPSTREAM = "https://api.511.org/transit/StopMonitoring";
const ROUTE = "/transit/StopMonitoring";
const ROUTE_HEALTH = "/health";

const UPSTREAM_GTFS = "https://api.511.org/transit/tripupdates";
const ROUTE_GTFS = "/gtfs/stops";
const ROUTE_GTFS_HEALTH = "/gtfs/health";

const DEFAULTS = { ttl: 120, hourlyLimit: 450, stale: 900, poll: 60, horizon: 5400, quietPoll: 180, quietFrom: 0, quietTo: 9 };

/// How long a stale copy is parked under the fresh key during a shortfall,
/// before anything tries the budget again. Short enough that recovery is quick
/// once tokens are available, long enough that a dashboard refreshing several
/// stops at once is not re-asking on every tick.
const BACKOFF_SECONDS = 10;

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (url.pathname === ROUTE_HEALTH) {
      return health(env);
    }

    if (url.pathname === ROUTE_GTFS_HEALTH) {
      return relay(env, "https://feed/health");
    }

    if (url.pathname === ROUTE_GTFS) {
      // Many stops in one call, because the whole city is already in memory and
      // a station board wants every platform at once. The cap is there so a
      // single request cannot be used to walk the entire feed out of us.
      const codes = (url.searchParams.get("codes") ?? "").split(",").filter(Boolean);
      if (codes.length === 0 || codes.length > 40) {
        return problem(400, "bad_codes", "codes must be 1-40 comma-separated stop codes.");
      }
      if (!codes.every((c) => /^[0-9]{1,10}$/.test(c))) {
        return problem(400, "bad_codes", "stop codes must be 1-10 digits.");
      }
      return relay(env, `https://feed/stops?codes=${codes.join(",")}`);
    }

    if (url.pathname !== ROUTE) {
      return problem(404, "not_found", `Nothing here. Try GET ${ROUTE}?agency=SF&stopcode=<digits>`);
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return problem(405, "method_not_allowed", "GET or HEAD only.", { Allow: "GET, HEAD" });
    }

    // Validated before anything reaches 511. This repo is public and the
    // endpoint takes no auth, so without this the Worker is a free relay for
    // arbitrary 511 calls made on our key.
    const stopcode = url.searchParams.get("stopcode") ?? "";
    const agency = url.searchParams.get("agency") ?? "SF";
    if (!/^[0-9]{1,10}$/.test(stopcode)) {
      return problem(400, "bad_stopcode", "stopcode must be 1-10 digits.");
    }
    if (agency !== "SF") {
      return problem(400, "bad_agency", "agency must be SF.");
    }

    const ttl = int(env.TTL_SECONDS, DEFAULTS.ttl);
    const staleTtl = int(env.STALE_SECONDS, DEFAULTS.stale);
    const cache = caches.default;
    const freshKey = cacheKey(stopcode, "fresh");
    const staleKey = cacheKey(stopcode, "stale");

    const fresh = await cache.match(freshKey);
    if (fresh) return serve(fresh, "HIT");

    // Cache miss. Every upstream call has to be paid for out of the hourly
    // budget, so ask before spending.
    const grant = await takeToken(env, stopcode);

    if (grant.ok) {
      const upstream = await callUpstream(stopcode, agency, env.API_511_KEY);
      if (upstream && upstream.ok) {
        const body = await upstream.arrayBuffer();
        const type = upstream.headers.get("content-type") ?? "application/json; charset=utf-8";
        ctx.waitUntil(Promise.all([
          cache.put(freshKey, storable(body, type, ttl)),
          cache.put(staleKey, storable(body, type, staleTtl)),
        ]));
        log(stopcode, "MISS", `upstream=200 budget=${fmt(grant.remaining)}`);
        return serve(storable(body, type, ttl), "MISS", grant.remaining);
      }
      log(stopcode, "UPSTREAM_FAIL", `status=${upstream ? upstream.status : "network-error"}`);
    }

    // Budget spent, or 511 let us down. Stale predictions are still useful:
    // arrival times are absolute timestamps and the app counts down against the
    // current clock, so an old snapshot keeps ticking correctly. What ages is
    // the estimate behind it, not the countdown.
    const stale = await cache.match(staleKey);
    if (stale) {
      const body = await stale.arrayBuffer();
      const type = stale.headers.get("content-type") ?? "application/json; charset=utf-8";
      const fetchedAt = stale.headers.get("X-Fetched-At");

      // Park the stale copy under the fresh key for a few seconds.
      //
      // Without this, a budget shortfall makes every single request re-ask the
      // budget, get refused, and re-read the same stale entry — a stampede
      // doing no work, hammering one Durable Object, and keeping the bucket
      // pinned at empty so genuine cold misses have nothing left to draw on.
      // Its original timestamp rides along, so the copy still reports its true
      // age rather than looking freshly baked.
      ctx.waitUntil(cache.put(freshKey, storable(body, type, BACKOFF_SECONDS, fetchedAt)));

      log(stopcode, "STALE", grant.ok ? "upstream failed" : `budget spent (${fmt(grant.remaining)})`);
      return serve(storable(body, type, BACKOFF_SECONDS, fetchedAt), "STALE", grant.remaining);
    }

    log(stopcode, "EMPTY", grant.ok ? "upstream failed, no stale copy" : "budget spent, no stale copy");
    return problem(503, "no_data", "No predictions available for this stop right now.", {
      "Retry-After": String(ttl),
    });
  },
};

/**
 * The hourly ceiling on upstream calls, as a token bucket that refills
 * continuously rather than resetting on the hour. A fixed window would let a
 * burst drain the whole allowance in five minutes and leave the next
 * fifty-five with nothing.
 *
 * One global instance. At this budget it handles a few hundred writes an hour
 * against a free-plan allowance of 100k a day, and it is only consulted on a
 * cache miss.
 */
export class Budget {
  constructor(state) {
    this.state = state;
  }

  async fetch(request) {
    const url = new URL(request.url);
    const cap = int(url.searchParams.get("limit"), DEFAULTS.hourlyLimit);
    const now = Date.now();
    const bucket = (await this.state.storage.get("bucket")) ?? { tokens: cap, updated: now };

    const elapsed = Math.max(0, (now - bucket.updated) / 1000);
    const tokens = Math.min(cap, bucket.tokens + elapsed * (cap / 3600));

    // Which stops are spending the budget, and how hard.
    //
    // Worth carrying: the ceiling is shared, so "we are at the ceiling" says
    // nothing about whether that is six stops behaving or two misbehaving, and
    // those need opposite fixes. Reset on the hour so it cannot grow without
    // bound.
    let spend = (await this.state.storage.get("spend")) ?? { since: now, stops: {} };
    if (now - spend.since >= 3_600_000) spend = { since: now, stops: {} };

    if (url.pathname === "/peek") {
      const stops = Object.entries(spend.stops).sort((a, b) => b[1] - a[1]);
      const total = stops.reduce((sum, [, n]) => sum + n, 0);
      const minutes = Math.max(1, (now - spend.since) / 60000);
      return json({
        ok: tokens >= 1,
        remaining: tokens,
        capacity: cap,
        spend: {
          windowMinutes: Math.round(minutes),
          distinctStops: stops.length,
          grants: total,
          perHour: Math.round(total / (minutes / 60)),
          top: Object.fromEntries(stops.slice(0, 12)),
        },
      });
    }

    if (url.pathname === "/note") {
      return this.note(url, tokens, cap, spend, now);
    }

    if (tokens < 1) {
      // Deliberately no write on refusal: the refill is derived from `updated`,
      // so leaving it alone keeps the arithmetic right and stops a hammering
      // client from turning every rejected request into a storage write.
      return json({ ok: false, remaining: tokens });
    }

    const stop = url.searchParams.get("stop") ?? "unknown";
    spend.stops[stop] = (spend.stops[stop] ?? 0) + 1;

    await this.state.storage.put("bucket", { tokens: tokens - 1, updated: now });
    await this.state.storage.put("spend", spend);
    return json({ ok: true, remaining: tokens - 1 });
  }

  /**
   * Record a call that has already been decided on, rather than asking
   * permission for one.
   *
   * The GTFS poller runs at a fixed rate that does not vary with how many
   * clients are connected, so there is nothing about it to ration -- but its
   * calls are real and they come out of the same 500/hr key. Left uncounted,
   * /health reports a budget that looks healthier than the key actually is.
   *
   * It deliberately does not gate. Gating would make a busy SIRI path able to
   * starve the feed that serves every stop in the city, which is backwards:
   * the poller is the cheap path and should have priority. Draining the shared
   * bucket gives it exactly that, since whatever it spends is no longer there
   * for per-stop SIRI calls to take.
   */
  async note(url, tokens, cap, spend, now) {
    const stop = url.searchParams.get("stop") ?? "unknown";
    spend.stops[stop] = (spend.stops[stop] ?? 0) + 1;
    const left = Math.max(0, tokens - 1);
    await this.state.storage.put("bucket", { tokens: left, updated: now });
    await this.state.storage.put("spend", spend);
    return json({ ok: true, remaining: left });
  }
}

// MARK: - GTFS-Realtime

async function relay(env, target) {
  try {
    const res = await env.FEED.get(env.FEED.idFromName("global")).fetch(target);
    const headers = new Headers(res.headers);
    headers.set("Access-Control-Allow-Origin", "*");
    headers.set("Cache-Control", "no-store");
    return new Response(res.body, { status: res.status, headers });
  } catch (err) {
    console.log(`feed unavailable: ${err}`);
    return json({ error: "unavailable", message: "Prediction feed is not reachable." }, 503);
  }
}

/**
 * Holds the whole city's predictions in memory.
 *
 * The whole inverted feed is about 570 KB — every arrival at every stop in
 * San Francisco — so it lives in memory and is served from there.
 *
 * Memory alone is not enough, which took production to discover. A Durable
 * Object with a pending alarm is NOT kept resident: the alarm schedules a
 * wake-up, it does not pin the object, and an idle one is evicted after about
 * ten seconds (the alarm invocation's own wallTimeMs lands on 10001, which is
 * that eviction showing through). With a 60 s poll the object is therefore
 * cold almost every time anyone asks, `polls` never reached 2 across a
 * five-minute watch, and a client request on a cold instance was re-fetching
 * 683 KB from 511 on its own latency — 877 ms against 73 ms warm, and one
 * upstream call per request, which is precisely the coupling this path exists
 * to remove.
 *
 * So each poll also writes the raw feed to durable storage, and a cold
 * instance restores from there and re-scans instead of going upstream. The raw
 * protobuf is stored rather than the inverted map because scan() is then the
 * only reader of the format and there is no second encoding to keep correct.
 */
export class Feed {
  constructor(state, env) {
    this.state = state;
    this.env = env;
    this.stops = new Map();   // stopcode (int) -> [route, dir, epoch, ...]
    this.fetchedAt = 0;       // ms, when the held copy came back from 511
    this.lastError = null;
    this.polls = 0;
    this.upstreamCalls = 0;   // calls this instance made to 511
    this.restoredFrom = null; // "storage" when this instance never went upstream
  }

  /// Chunk width for the stored snapshot.
  ///
  /// Sized against the SQLite backend's ceiling -- key and value combined
  /// cannot exceed 2 MB -- not the key-value backend's 128 KiB, which is what
  /// the old 96 KB was clearing and which this Worker cannot use anyway (the
  /// KV backend is paid-only; see the migrations in wrangler.toml).
  ///
  /// Width is a row-count knob, and rows are the scarce resource here: the
  /// free plan allows 100k writes a day across the whole account. Counting the
  /// meta row, one poll of a 1.6 MB rush-hour feed costs 3 rows at this width
  /// against 18 at 96 KB; the 1,091 KB feed measured at midday costs 3 against
  /// 13. Over ~1,080 polls a day that is on the order of 12-16k rows back,
  /// something like an eighth of the daily allowance -- the low end, because
  /// the overnight feeds that fill the quiet window are smaller than either.
  ///
  /// Widening is backward compatible: restore() trusts meta.chunks and
  /// reassembles by length, so it reads a snapshot written at any width.
  static get CHUNK() { return 1024 * 1024; }

  async fetch(request) {
    const url = new URL(request.url);

    // The alarm is what keeps this warm; arm it on the first request to touch
    // this object after a cold start, and let it re-arm itself from then on.
    if ((await this.state.storage.getAlarm()) === null) {
      await this.state.storage.setAlarm(Date.now() + 100);
    }

    if (url.pathname === "/health") {
      return json({
        ok: this.stops.size > 0,
        stops: this.stops.size,
        arrivals: this.arrivalCount(),
        ageSeconds: this.fetchedAt ? Math.round((Date.now() - this.fetchedAt) / 1000) : null,
        polls: this.polls,
        upstreamCalls: this.upstreamCalls,
        restoredFrom: this.restoredFrom,
        pollSeconds: this.pollInterval(),
        quietHours: {
          active: this.isQuietHour(),
          pacificHour: pacificHour(),
          from: hour(this.env.QUIET_FROM_HOUR, DEFAULTS.quietFrom),
          to: hour(this.env.QUIET_TO_HOUR, DEFAULTS.quietTo),
          pollSeconds: int(this.env.QUIET_POLL_SECONDS, DEFAULTS.quietPoll),
        },
        lastError: this.lastError,
      });
    }

    // A cold object has an armed alarm but no data yet. Restoring the last
    // stored feed costs a storage read and a re-scan; going upstream costs
    // most of a second and a call on the shared key. Only do the latter when
    // there is genuinely nothing stored, which is the first request after a
    // deploy and not much else.
    await this.ensureLoaded();

    const codes = (url.searchParams.get("codes") ?? "").split(",").filter(Boolean);
    const now = Math.floor(Date.now() / 1000);
    const out = {};
    for (const code of codes) {
      const flat = this.stops.get(Number(code));
      if (!flat) { out[code] = []; continue; }
      const rows = [];
      for (let i = 0; i < flat.length; i += 3) {
        if (flat[i + 2] >= now) rows.push({ route: flat[i], dir: flat[i + 1], at: flat[i + 2] });
      }
      rows.sort((a, b) => a.at - b.at);
      out[code] = rows.slice(0, 8);
    }

    const age = this.fetchedAt ? Math.round((Date.now() - this.fetchedAt) / 1000) : null;
    return json({ now, ageSeconds: age, stops: out }, this.stops.size ? 200 : 503, {
      "X-Feed-Age": String(age ?? -1),
    });
  }

  async alarm() {
    // Re-arm first. A throw inside poll() must not leave this object with no
    // alarm pending, which would silently stop the whole feed until the next
    // client request happened to wake it.
    await this.state.storage.setAlarm(Date.now() + this.pollInterval() * 1000);
    await this.poll();
  }

  /**
   * Seconds until the next poll.
   *
   * TEMPORARY -- this whole quiet-window idea exists because /gtfs is a test
   * deployment with an audience of one. Between QUIET_FROM_HOUR and
   * QUIET_TO_HOUR Pacific nobody is looking at it, so it polls at a third of
   * the rate rather than spending the key on predictions no one will read.
   *
   * Note what this is NOT: it is not a claim that predictions matter less in
   * the early morning. The tail of that window is the start of the commute,
   * and riders on the first N of the day need this more than anyone. They are
   * served by the SIRI path, which this does not touch. The day /gtfs starts
   * answering real clients, delete this method and go back to a flat interval
   * -- see the note in wrangler.toml.
   */
  pollInterval() {
    const fast = int(this.env.POLL_SECONDS, DEFAULTS.poll);
    const slow = int(this.env.QUIET_POLL_SECONDS, DEFAULTS.quietPoll);
    return this.isQuietHour() ? slow : fast;
  }

  /// `now` is injectable so the window can be tested against all 24 hours
  /// without waiting for the clock to reach them.
  isQuietHour(now = pacificHour()) {
    const from = hour(this.env.QUIET_FROM_HOUR, DEFAULTS.quietFrom);
    const to = hour(this.env.QUIET_TO_HOUR, DEFAULTS.quietTo);
    // A window that wraps past midnight (22 -> 6) is the union of two ranges,
    // not an empty one, so it is worth handling even though the default
    // window does not wrap.
    return from <= to ? now >= from && now < to : now >= from || now < to;
  }

  /// Memory, then storage, then 511 -- in that order, because that is the
  /// order of what they cost.
  async ensureLoaded() {
    if (this.stops.size > 0) return;
    if (await this.restore()) return;
    await this.poll();
  }

  /**
   * Rebuild from the last stored feed.
   *
   * The re-scan is the same code the alarm runs, so a restored instance holds
   * exactly what a freshly polled one would, minus whatever changed upstream
   * since. Absent or partial snapshots return false rather than throwing, and
   * the caller falls through to a real poll.
   *
   * This is the CPU-constrained path, and the only one. It runs inside a
   * client request, so it gets the free plan's 10 ms, not the 30 s an alarm
   * gets -- and it does the two most expensive things in the file: it
   * reassembles the whole feed with bytes.set, then scans it. Scaling the
   * measured 1.81 ms at 683 KB, a 1.6 MB rush-hour feed is ~4.3 ms of scan
   * alone at the current 45 min horizon, and ~9.2 ms at the 90 min horizon
   * this used to run. So HORIZON_SECONDS cannot simply go back up: the alarm
   * would not notice, but this would, and 1102 here is a 5xx to a rider.
   *
   * The way out, if it ever needs one, is for the alarm to store the inverted
   * map instead of the raw feed, which would move the scan onto the invocation
   * with 30 s of headroom and leave this one a deserialize. That is the second
   * encoding the class note argues against, and the argument still stands --
   * it is written here only so the option is not rediscovered under pressure.
   */
  async restore() {
    try {
      const meta = await this.state.storage.get("snap:meta");
      if (!meta || !meta.chunks) return false;

      const keys = [];
      for (let i = 0; i < meta.chunks; i++) keys.push(`snap:${i}`);
      const parts = await this.state.storage.get(keys);

      const bytes = new Uint8Array(meta.bytes);
      let offset = 0;
      for (const key of keys) {
        const part = parts.get(key);
        if (!part) return false;   // torn snapshot; go upstream instead
        bytes.set(part, offset);
        offset += part.length;
      }
      if (offset !== meta.bytes) return false;

      const stops = scan(bytes, this.horizon());
      if (stops.size === 0) return false;

      this.stops = stops;
      this.fetchedAt = meta.fetchedAt;
      this.restoredFrom = "storage";
      console.log(`restored ${meta.bytes}B -> ${stops.size} stops, age ${Math.round((Date.now() - meta.fetchedAt) / 1000)}s`);
      return true;
    } catch (err) {
      console.log(`restore failed: ${err}`);
      return false;
    }
  }

  async poll() {
    const url = new URL(UPSTREAM_GTFS);
    url.searchParams.set("api_key", this.env.API_511_KEY);
    url.searchParams.set("agency", "SF");

    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(10_000) });
      this.upstreamCalls++;
      // Recorded, not requested. See Budget.note(): this call is already made
      // and the poller must not be gated, but it does come out of the same
      // 500/hr key and /health should say so.
      await noteToken(this.env, "gtfs-feed");

      if (!res.ok) {
        this.lastError = `upstream ${res.status}`;
        console.log(`poll failed: upstream ${res.status}`);
        return;
      }
      const bytes = new Uint8Array(await res.arrayBuffer());

      const stops = scan(bytes, this.horizon());
      if (stops.size === 0) {
        // A structurally valid but empty feed should not blank out a good copy.
        this.lastError = "feed parsed to zero stops";
        return;
      }

      this.stops = stops;
      this.fetchedAt = Date.now();
      this.lastError = null;
      this.restoredFrom = null;
      this.polls++;
      console.log(`poll ok: ${bytes.length}B -> ${stops.size} stops`);
      await this.store(bytes);
    } catch (err) {
      this.lastError = String(err);
      console.log(`poll threw: ${err}`);
    }
  }

  /**
   * Park the raw feed so the next cold instance does not have to go upstream.
   *
   * What this costs is rows, not CPU. The old note here said the cost "never
   * lands on a client", which was true of latency and beside the point: this
   * is awaited inside alarm(), and an alarm on a sub-hour interval has 30 s of
   * CPU, so the milliseconds were never the constraint. Writes are -- the free
   * plan allows 100k rows a day across the account, and at the old 96 KB chunk
   * width a rush-hour feed spent 18 of them every 60 s.
   *
   * The meta row is written last, so a snapshot only becomes visible once
   * every chunk of it is in place.
   */
  async store(bytes) {
    try {
      const prev = await this.state.storage.get("snap:meta");

      const chunk = Feed.CHUNK;
      const n = Math.ceil(bytes.length / chunk);
      const batch = {};
      for (let i = 0; i < n; i++) batch[`snap:${i}`] = bytes.slice(i * chunk, (i + 1) * chunk);
      await this.state.storage.put(batch);
      await this.state.storage.put("snap:meta", { chunks: n, bytes: bytes.length, fetchedAt: this.fetchedAt });

      // A snapshot that shrank leaves the tail of the previous one behind --
      // from a smaller feed, or from a deploy that widened CHUNK. restore()
      // never reads those rows, since it trusts meta.chunks, but they do sit
      // in storage, so drop them on the polls where the count actually falls.
      // Deleted after meta, so a failure here cannot strand a torn snapshot.
      if (prev && prev.chunks > n) {
        const stale = [];
        for (let i = n; i < prev.chunks; i++) stale.push(`snap:${i}`);
        await this.state.storage.delete(stale);
      }
    } catch (err) {
      // A feed that cannot be parked is still a feed that can be served.
      console.log(`store failed: ${err}`);
    }
  }

  /// Arrivals beyond this are for vehicles that have not started their run.
  /// The scan uses it to abandon a trip early, so it is a CPU knob as much as
  /// a content one.
  horizon() {
    return Math.floor(Date.now() / 1000) + int(this.env.HORIZON_SECONDS, DEFAULTS.horizon);
  }

  arrivalCount() {
    let n = 0;
    for (const v of this.stops.values()) n += v.length / 3;
    return n;
  }
}

/**
 * Reads GTFS-Realtime TripUpdates off the wire, keeping only route_id,
 * direction_id, stop_id and arrival.time.
 *
 * Field numbers, for anyone checking this against the spec:
 *   FeedMessage.entity = 2          FeedEntity.trip_update = 3
 *   TripUpdate.trip = 1, stop_time_update = 2
 *   TripDescriptor.route_id = 5, direction_id = 6
 *   StopTimeUpdate.arrival = 2, stop_id = 4
 *   StopTimeEvent.time = 2
 *
 * Two things are load-bearing for the CPU budget and look odd without that
 * context. Varints are read with an inline single-byte fast path rather than a
 * helper call, because nearly every tag and length in this feed is one byte and
 * the feed contains hundreds of thousands of them. And stop ids are folded from
 * ASCII digits straight to integers, which skips ~24,000 short-lived string
 * allocations per poll. Measured together these take the scan from 7.5 ms to
 * 4.1 ms.
 *
 * Worth keeping, but not for the reason first written here. The note used to
 * say this was "the difference between fitting in the free plan and not",
 * meaning the 10 ms ceiling, and the alarm is not where that ceiling lives: a
 * Durable Object alarm on a sub-hour interval gets 30 s. The 10 ms limit is
 * per HTTP request, so what it governs is restore(), which runs this same scan
 * inside a cold client request. That path is the one with a real budget, and
 * at rush-hour feed sizes it is not comfortable in it -- see the note on
 * restore().
 *
 * Boundaries are always read in two steps -- `const n = varint(); const end =
 * p + n;` -- never `p + varint()`. The one-liner reads `p` before the varint
 * advances it, so every submessage ends up short by the width of its own length
 * prefix, and the walk drifts into garbage a few hundred entities in.
 */
export function scan(buf, horizon) {
  if (horizon === undefined) horizon = Infinity;
  const byStop = new Map();
  const len = buf.length;
  let p = 0;

  while (p < len) {
    let b = buf[p++], k = b;
    if (b & 0x80) { k = b & 0x7f; let s = 7, c; do { c = buf[p++]; k += (c & 0x7f) * 2 ** s; s += 7; } while (c & 0x80); }
    const w = k & 7;
    if (w !== 2) { if (w === 0) { while (buf[p++] & 0x80); } else if (w === 5) p += 4; else if (w === 1) p += 8; continue; }
    let n = buf[p++]; if (n & 0x80) { n &= 0x7f; let s = 7, c; do { c = buf[p++]; n += (c & 0x7f) * 2 ** s; s += 7; } while (c & 0x80); }
    const entEnd = p + n;
    if ((k >>> 3) !== 2) { p = entEnd; continue; }

    while (p < entEnd) {                                          // FeedEntity
      let b2 = buf[p++], k2 = b2;
      if (b2 & 0x80) { k2 = b2 & 0x7f; let s = 7, c; do { c = buf[p++]; k2 += (c & 0x7f) * 2 ** s; s += 7; } while (c & 0x80); }
      const w2 = k2 & 7;
      if (w2 !== 2) { if (w2 === 0) { while (buf[p++] & 0x80); } else if (w2 === 5) p += 4; else if (w2 === 1) p += 8; continue; }
      let n2 = buf[p++]; if (n2 & 0x80) { n2 &= 0x7f; let s = 7, c; do { c = buf[p++]; n2 += (c & 0x7f) * 2 ** s; s += 7; } while (c & 0x80); }
      const tuEnd = p + n2;
      if ((k2 >>> 3) !== 3) { p = tuEnd; continue; }

      let route = "", dir = 0;
      while (p < tuEnd) {                                         // TripUpdate
        let b3 = buf[p++], k3 = b3;
        if (b3 & 0x80) { k3 = b3 & 0x7f; let s = 7, c; do { c = buf[p++]; k3 += (c & 0x7f) * 2 ** s; s += 7; } while (c & 0x80); }
        const w3 = k3 & 7, f3 = k3 >>> 3;
        if (w3 !== 2) { if (w3 === 0) { while (buf[p++] & 0x80); } else if (w3 === 5) p += 4; else if (w3 === 1) p += 8; continue; }
        let n3 = buf[p++]; if (n3 & 0x80) { n3 &= 0x7f; let s = 7, c; do { c = buf[p++]; n3 += (c & 0x7f) * 2 ** s; s += 7; } while (c & 0x80); }
        const subEnd = p + n3;

        if (f3 === 2) {                                           // StopTimeUpdate
          let stop = -1, time = 0;
          while (p < subEnd) {
            let b4 = buf[p++], k4 = b4;
            if (b4 & 0x80) { k4 = b4 & 0x7f; let s = 7, c; do { c = buf[p++]; k4 += (c & 0x7f) * 2 ** s; s += 7; } while (c & 0x80); }
            const w4 = k4 & 7, f4 = k4 >>> 3;
            if (w4 === 0) { while (buf[p++] & 0x80); continue; }
            if (w4 !== 2) { if (w4 === 5) p += 4; else if (w4 === 1) p += 8; continue; }
            let n4 = buf[p++]; if (n4 & 0x80) { n4 &= 0x7f; let s = 7, c; do { c = buf[p++]; n4 += (c & 0x7f) * 2 ** s; s += 7; } while (c & 0x80); }
            const evEnd = p + n4;
            if (f4 === 4) { let v = 0; for (let i = p; i < evEnd; i++) v = v * 10 + (buf[i] - 48); stop = v; }
            else if (f4 === 2) {                                  // arrival
              while (p < evEnd) {
                let b5 = buf[p++], k5 = b5;
                if (b5 & 0x80) { k5 = b5 & 0x7f; let s = 7, c; do { c = buf[p++]; k5 += (c & 0x7f) * 2 ** s; s += 7; } while (c & 0x80); }
                const w5 = k5 & 7;
                if (w5 === 0) {
                  let v = buf[p++]; if (v & 0x80) { v &= 0x7f; let s = 7, c; do { c = buf[p++]; v += (c & 0x7f) * 2 ** s; s += 7; } while (c & 0x80); }
                  if ((k5 >>> 3) === 2) time = v;
                } else if (w5 === 2) { let m = buf[p++]; if (m & 0x80) { m &= 0x7f; let s = 7, c; do { c = buf[p++]; m += (c & 0x7f) * 2 ** s; s += 7; } while (c & 0x80); } p += m; }
                else if (w5 === 5) p += 4; else if (w5 === 1) p += 8;
              }
            }
            p = evEnd;
          }
          if (time && stop >= 0) {
            // Arrival times inside a trip are monotonic -- a vehicle visits its
            // stops in order -- so the first arrival past the horizon means
            // every remaining stop_time_update in this trip is past it too.
            // Jumping to the end of the trip skips tags that would only be
            // read and discarded, and it is the only way to make the walk
            // cheaper: protobuf has no index, so a field can never be skipped
            // without first reading its tag and length. Filtering alone saves
            // nothing measurable (3.74 ms vs 3.88 ms at a 45 min horizon);
            // this takes the same scan to 1.81 ms, with identical output.
            // Verified monotonic across all 466 trips in a live feed.
            if (time > horizon) { p = tuEnd; break; }
            let v = byStop.get(stop);
            if (v === undefined) { v = []; byStop.set(stop, v); }
            v.push(route, dir, time);
          }
        } else if (f3 === 1) {                                    // TripDescriptor
          while (p < subEnd) {
            let b4 = buf[p++], k4 = b4;
            if (b4 & 0x80) { k4 = b4 & 0x7f; let s = 7, c; do { c = buf[p++]; k4 += (c & 0x7f) * 2 ** s; s += 7; } while (c & 0x80); }
            const w4 = k4 & 7, f4 = k4 >>> 3;
            if (w4 === 2) {
              let n4 = buf[p++]; if (n4 & 0x80) { n4 &= 0x7f; let s = 7, c; do { c = buf[p++]; n4 += (c & 0x7f) * 2 ** s; s += 7; } while (c & 0x80); }
              if (f4 === 5) { let r = ""; for (let i = p; i < p + n4; i++) r += String.fromCharCode(buf[i]); route = r; }
              p += n4;
            } else if (w4 === 0) {
              let v = buf[p++]; if (v & 0x80) { v &= 0x7f; let s = 7, c; do { c = buf[p++]; v += (c & 0x7f) * 2 ** s; s += 7; } while (c & 0x80); }
              if (f4 === 6) dir = v;
            } else if (w4 === 5) p += 4; else if (w4 === 1) p += 8;
          }
        }
        p = subEnd;
      }
      p = tuEnd;
    }
    p = entEnd;
  }
  return byStop;
}

/**
 * Whether this Worker is actually wired up, without saying anything about what
 * the bindings contain.
 *
 * Worth having permanently: a missing binding and a refused budget both end as
 * the same 503 on the predictions route, and the two want very different
 * fixes. Reports booleans only — never a value, never a length.
 */
async function health(env) {
  const bindings = {
    API_511_KEY: typeof env.API_511_KEY === "string" && env.API_511_KEY.length > 0,
    BUDGET: Boolean(env.BUDGET),
  };

  let budget = null;
  if (bindings.BUDGET) {
    try {
      const limit = int(env.HOURLY_LIMIT, DEFAULTS.hourlyLimit);
      const res = await env.BUDGET.get(env.BUDGET.idFromName("global"))
        .fetch(`https://budget/peek?limit=${limit}`);
      budget = await res.json();
    } catch (err) {
      budget = { error: String(err) };
    }
  }

  return json({
    ok: bindings.API_511_KEY && bindings.BUDGET && Boolean(budget) && !budget.error,
    bindings,
    config: {
      ttl: int(env.TTL_SECONDS, DEFAULTS.ttl),
      hourlyLimit: int(env.HOURLY_LIMIT, DEFAULTS.hourlyLimit),
      stale: int(env.STALE_SECONDS, DEFAULTS.stale),
    },
    budget,
  }, 200, { "Cache-Control": "no-store", "Access-Control-Allow-Origin": "*" });
}

// MARK: - Upstream

async function callUpstream(stopcode, agency, apiKey) {
  const url = new URL(UPSTREAM);
  url.searchParams.set("api_key", apiKey);
  url.searchParams.set("agency", agency);
  url.searchParams.set("stopcode", stopcode);
  url.searchParams.set("format", "json");

  try {
    return await fetch(url, {
      headers: { "Accept-Encoding": "gzip" },
      signal: AbortSignal.timeout(10_000),
    });
  } catch (err) {
    console.log(`stop=${stopcode} upstream threw: ${err}`);
    return null;
  }
}

/// Tell the budget a call has happened. Never gates, never throws: a poller
/// that cannot reach the bookkeeping should still serve predictions.
async function noteToken(env, label) {
  try {
    const id = env.BUDGET.idFromName("global");
    const limit = int(env.HOURLY_LIMIT, DEFAULTS.hourlyLimit);
    await env.BUDGET.get(id).fetch(`https://budget/note?limit=${limit}&stop=${label}`);
  } catch (err) {
    console.log(`budget note failed: ${err}`);
  }
}

async function takeToken(env, stopcode) {
  try {
    const id = env.BUDGET.idFromName("global");
    const limit = int(env.HOURLY_LIMIT, DEFAULTS.hourlyLimit);
    const res = await env.BUDGET.get(id).fetch(`https://budget/take?limit=${limit}&stop=${stopcode}`);
    return await res.json();
  } catch (err) {
    // A broken budget must not take the endpoint down with it. Falling through
    // to the stale copy is the safe failure: it serves readers without risking
    // an uncounted stampede at 511.
    console.log(`stop=${stopcode} budget unavailable: ${err}`);
    return { ok: false, remaining: 0 };
  }
}

// MARK: - Cache

/**
 * Cache entries are addressed by a URL we invent, not by the request that came
 * in. On a custom domain the query string is not reliably part of the default
 * cache key, and a key that ignores it would hand one stop's departures to
 * another — wrong in a way that still looks like a working board.
 */
function cacheKey(stopcode, kind) {
  return new Request(`https://cache.invalid/stop/${stopcode}/${kind}`);
}

/**
 * A response built for storage.
 *
 * Headers are rebuilt from nothing rather than copied. 511 answers with
 * `Cache-Control: no-cache`, `Pragma: no-cache` and `Expires: -1`, and
 * `cache.put` honours all three — pass its response through unedited and the
 * cache silently stores nothing while every request goes upstream.
 */
function storable(body, contentType, maxAge, fetchedAt) {
  return new Response(body, {
    status: 200,
    headers: {
      "Content-Type": contentType,
      "Cache-Control": `public, max-age=${maxAge}`,
      // Carried over when re-parking an older copy, so its age keeps counting
      // from when 511 produced it rather than resetting every time it is
      // handed out again.
      "X-Fetched-At": fetchedAt ?? new Date().toISOString(),
    },
  });
}

/**
 * A stored response on its way back out.
 *
 * `no-store` on the way to the client is deliberate: this Worker's cache is the
 * one caching layer, and letting URLSession keep its own copy on top would add
 * a second, invisible amount of staleness to reason about.
 */
function serve(response, state, remaining) {
  const headers = new Headers(response.headers);
  const fetchedAt = headers.get("X-Fetched-At");

  headers.set("X-Cache", state);
  headers.set("Cache-Control", "no-store");
  headers.set("Access-Control-Allow-Origin", "*");
  if (fetchedAt) {
    headers.set("Age", String(Math.max(0, Math.round((Date.now() - Date.parse(fetchedAt)) / 1000))));
  }
  if (remaining !== undefined) {
    headers.set("X-Budget-Remaining", fmt(remaining));
  }
  return new Response(response.body, { status: response.status, headers });
}

// MARK: - Helpers

function json(value, status = 200, extra = {}) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...extra },
  });
}

function problem(status, code, message, extra = {}) {
  return json({ error: code, message }, status, {
    "Access-Control-Allow-Origin": "*",
    "Cache-Control": "no-store",
    ...extra,
  });
}

/**
 * The hour of the day in San Francisco, 0-23.
 *
 * Pacific rather than UTC because the window it feeds is about when a person
 * here is awake, and DST would otherwise walk it an hour twice a year. The
 * Workers runtime carries full ICU, so the IANA zone is enough and no offset
 * arithmetic is needed. `hour12: false` reports midnight as 24 in some
 * implementations, hence the modulo.
 */
function pacificHour(now = new Date()) {
  const h = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Los_Angeles",
    hour: "numeric",
    hour12: false,
  }).format(now);
  return Number.parseInt(h, 10) % 24;
}

/// Like int(), but 0 is a legitimate hour, so it cannot use the >0 test.
function hour(value, fallback) {
  const n = Number.parseInt(value, 10);
  return Number.isFinite(n) && n >= 0 && n <= 23 ? n : fallback;
}

function int(value, fallback) {
  const n = Number.parseInt(value, 10);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

function fmt(n) {
  return String(Math.floor(Number(n) || 0));
}

function log(stopcode, state, detail) {
  console.log(`stop=${stopcode} cache=${state} ${detail}`);
}
