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
 * It deliberately never parses a large payload. Slicing 511's whole-agency feed
 * would be the obvious way to serve every stop from one upstream call, but that
 * feed is 27 MB and costs ~62 ms of CPU to parse, against a 10 ms per-invocation
 * ceiling on the Workers free plan — a ceiling that applies to Cron Triggers
 * too. 511 also ignores every parameter that would trim the feed and rejects
 * comma-separated stop codes, so there is no smaller version of it to ask for.
 * Requests here are passed through byte for byte instead, which keeps CPU near
 * 1 ms and leaves the app's existing decoder untouched.
 */

const UPSTREAM = "https://api.511.org/transit/StopMonitoring";
const ROUTE = "/transit/StopMonitoring";
const ROUTE_HEALTH = "/health";

const DEFAULTS = { ttl: 45, hourlyLimit: 450, stale: 900 };

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

    // A look at the bucket that doesn't spend from it, for /health.
    if (url.pathname === "/peek") {
      return json({ ok: tokens >= 1, remaining: tokens, capacity: cap });
    }

    if (tokens < 1) {
      // Deliberately no write on refusal: the refill is derived from `updated`,
      // so leaving it alone keeps the arithmetic right and stops a hammering
      // client from turning every rejected request into a storage write.
      return json({ ok: false, remaining: tokens });
    }

    await this.state.storage.put("bucket", { tokens: tokens - 1, updated: now });
    return json({ ok: true, remaining: tokens - 1 });
  }
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

async function takeToken(env, stopcode) {
  try {
    const id = env.BUDGET.idFromName("global");
    const limit = int(env.HOURLY_LIMIT, DEFAULTS.hourlyLimit);
    const res = await env.BUDGET.get(id).fetch(`https://budget/take?limit=${limit}`);
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
