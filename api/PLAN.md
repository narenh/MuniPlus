# Muni+ backend — plan and contract

The written spec for everything under `api/`. Agents working on a track read this
first. The types in `app/models/` are the contract. Where this document and the
code disagree, the code is right and this document needs fixing.

## What we are building

A FastAPI server at `muni-staging.canopysf.com` that:

* serves station and line data for the whole SF system, built from **511** (the
  machine layer) plus **hand curation** (the human layer), both kept in the public
  data repo [`narenh/sf-transit`](https://github.com/narenh/sf-transit);
* polls 511's realtime feeds and serves arrivals, vehicle positions and alerts;
* hosts the station **editor** at `/editor` (password login) and a public,
  read-only copy of it at `/map`.

Only dev and simulator builds of the app talk to this server. The App Store build
keeps using the Cloudflare Worker (`../src/`) and `../appdata/`. **Those are
production and frozen: never edit them from this work.** `../tools/` is kept
read-only as a reference until the app moves over.

## Terms

* **stop**: one place a line stops, as 511 numbers it (`SF:16992`). What 511 and
  the realtime feeds speak in. Type `StopId`.
* **platform**: one place a rider stands. Usually one stop; where 511 numbers one
  place more than once (the N and its bus substitute on one Duboce shelter), several.
  Its id is its primary stop's; `stops` lists the others. Read a platform's stops
  through `Platform.all_stops` (JS `stopsOf`), never `id` alone.
* **station**: the platforms riders think of as one place, with a name and an id of
  our own.

## Decisions (settled — do not reopen without new evidence)

| topic | decision |
|---|---|
| stack | Python 3.13, FastAPI, Pydantic v2, SQLAlchemy on SQLite. **One uvicorn worker, enforced** by a poller lock in SQLite |
| deploy | Coolify builds `master` (base dir `/api`, watch paths `api/**`), persistent volume at `/data` |
| data repo | `sf-transit`: fresh history; the editor commits there via a deploy key; nothing redeploys on a data edit |
| vocabulary | **lines**, never routes, everywhere (511 says lines). GTFS "routes" are renamed at ingest. HTTP handler modules live in `app/endpoints/` |
| ids | `<511 operator>:<upstream id>` for stops and lines (`SF:16992`, `SF:LOWL`), no mapping table. A platform's id is its primary stop's. Station ids are ours, `[A-Za-z0-9]+`, permanent |
| renames | `mongomery` → `montgomery`; the old id lives in `formerIds` and is flagged as a client favourites migration |
| stations | one flat platform list; a platform may take in several stops (`stops`), curated by hand, never by distance. Levels/exits come later as an optional `layout` field. All hand-curated station info is in `curation/stations.json` |
| transfers | always both ways: one entry per pair of stations, in `stations.json`'s top-level `transfers` (`{between: [a, b], mode}`), so a one-way transfer cannot be written. Only the 12 pairs from the iOS app's data were kept; the generator's one-way links were dropped (2026-09-23) |
| coordinates | never curated. Stops take 511's; a platform is the centroid of its live stops, a station of all its live stops |
| modes | 511's line `TransportMode` verbatim (`metro`, `bus`, `cableway`). One curated override: the F is `streetcar` |
| replacements | `replaces` on a line (`SF:LOWL` replaces `SF:L`). **No** replacement platforms, no station-level mapping |
| terminates | **not curated.** Derived per trip from realtime: departure-only = the trip starts here; arrival-only last stop = the trip ends here |
| line diagrams | the most-run 511 pattern per direction, read-only in the editor; no manual reordering |
| new 511 stops | not in the API until assigned to a station (or ignored) in the editor's review queue |
| validation | one Python validator, run by the server for the loader and the editor; no copy in the browser |
| hubs | `hub: true` on a station marks a major metro interchange (4th & King, Union Square, Balboa Park). Curated, because a rule on line counts also catches every stop where two branches meet |
| verification | `verified` date per station. The seed marks hand-curated stations verified and generated bus stations unverified. A change to a station's platform list clears it |
| 511 budget | dev key: 60/hr. Arrivals 180 s, vehicles 180 s, alerts 1200 s = 43/hr. Client-enforced ceiling 55/hr, persisted |

## Data (sf-transit)

```
curation/stations.json   { subways: {id: {name, stations[]}}, stations: {id: Station} }
curation/lines.json      { lineId: LineOverride }     overrides only
curation/ignored.json    { stopId: {note} }           511 stops deliberately left out
curation/shapes.json     { patchId: {lines[], path[[lon, lat]...], note?} }   corrections to 511's shapes
snapshot/SF/meta.json    service period, fetch time, sha256 of the GTFS zip
snapshot/SF/stops.json   { stopId: {name, lat, lon} }
snapshot/SF/lines.json   { lineId: {shortName, longName, mode, routeType, color, textColor} }
snapshot/SF/patterns.json { lineId: [{direction, headsign, trips, stops[], shape?}] }
snapshot/SF/shapes.json  { shapeId: [[lon, lat], ...] }   only shapes a pattern names
```

Models: `app/models/curation.py`, `app/models/snapshot.py`. Reading and writing:
`app/models/files.py`, which is **the only code that writes these files**. Its
format is deterministic (sorted maps, unset fields omitted, two-space indent), so
a round trip is byte-identical and an unchanged save writes nothing.

A snapshot refresh for SF is two 511 calls: the GTFS zip (`/transit/datafeeds`)
and `/transit/lines` (for `mode`, which the zip lacks). Shapes come from the same
zip's `shapes.txt`: no extra call. A pattern's `shape` is the one most of its
trips drive; a feed without shapes gives none and the map draws through the stops. Checked 2026-09-22:
511's `stop_id` equals `stop_code` for all 3,240 SF stops, and matches realtime
`stop_id`s. All 68 lines carry colours.

### Derived at load time (never stored)

* stop: `lines` (union over patterns containing it), `lat`/`lon`/`name`
  (snapshot), `live` (present in snapshot)
* platform: `lines` (union over its stops), `lat`/`lon` (centroid of its live stops),
  live while any of its stops is
* station: `lat`/`lon` (centroid of its live stops), `lines`, `modes`
* line: `name` default `"{shortName} {Title Case longName}"`; `mode` (override,
  else snapshot); `directions` (the most-run pattern per direction, mapped to
  stations; unassigned stops dropped)

### Validation (`Issue.code` values are stable identifiers)

The rule for choosing: **nothing a snapshot refresh can cause is an error.** 511
dropping a stop or a line must never block every save until someone fixes it by
hand, so anything that depends on the snapshot is a warning.

Errors, which block a save:

* `duplicate-station-id`: two ids differing only in case (`powell` / `Powell`).
  An exact duplicate key cannot get this far: `files.py` refuses the file.
* `former-id-collides`: a former id reused, or equal to a current id
* `stop-in-two-stations`, `stop-listed-twice`, `stop-assigned-and-ignored` (a stop
  counts wherever a platform lists it, as its id or in its `stops`)
* `unknown-station`: a transfer or subway naming a station that does not exist
* `transfer-to-itself`, `duplicate-transfer` (the same pair twice, in either order)
* `station-has-no-platforms`

Warnings, which never block:

* `stop-not-in-snapshot` (dropped from the API output)
* `platform-stops-far-apart` (a platform's extra stop more than 25 m from its primary:
  two ids for one shelter sit metres apart)
* `station-has-no-live-platforms` (left out of the public API; still in `derived`)
* `unassigned-stop` (in the snapshot, in no station, not ignored: this is the review queue)
* `unknown-line-override` (`lines.json` names a line 511 does not have, e.g. `S`)
* `unknown-line` (`replaces` names a line 511 does not have)
* `shape-patch-unknown-line` (a shape patch names a line 511 does not have)
* `shape-patch-unmatched` (511's shape for a patched line no longer passes within 25 m of both ends of the patch; 511's path is drawn instead)

### Shape patches

Where 511's shape is wrong (the T's dogleg at Market & 4th), `curation/shapes.json`
holds the real path for that stretch. A patch's first and last points sit on 511's
shape. Every drawn shape of the listed lines that passes within 25 m of both ends
has the stretch between them replaced, with the path reversed for the opposite
direction. Patches are keyed to the geometry, not to shape ids, so they keep
applying after a refresh renumbers the shapes. They are applied before `/api/shapes`
simplifies and hashes the shapes, so a patch changes that response's ETag.

A patch with one end on a line's first or last point and the other off its shape
is an **extension**, joined on at that end after the shapes are cut to their
terminal stops (which would otherwise remove it). The J and K at Balboa Park end
at their boarding stop, 75 m short of the station; an extension carries them to it.

Ids, headings and non-blank names are already enforced by the models; duplicate
JSON keys by `files.loads`.

`Derived.stops` covers every stop a platform names **and** every snapshot stop, so
the review queue can show what serves an unassigned stop. There `live` means "in
the snapshot", so an unassigned stop is `live: true`. `Network.is_live` is
stricter: assigned *and* in the snapshot. `Derived.lines` carries full
`LineDetail`, directions included.

## API

Response models: `app/models/api.py`. camelCase. Realtime times are epoch seconds.

Versioned: everything public is under `/api/v1/`. Within v1 changes are additive only; a breaking change is `/api/v2/`, served alongside. ETags carry a hash of the response schemas as well as the data version, so a deploy that changes a shape never answers an old-shaped cache with a 304. Realtime answers carry `refreshAfter`, seconds until the server can have newer data: clients poll on that, never on a constant.

| endpoint | notes |
|---|---|
| `GET /api/v1/stations` | summaries (with `operators`) + `formerIds` map + `version`; ETag = `version`.`schemaHash`, `Cache-Control: no-cache` |
| `GET /api/v1/stations/{id}` | detail; a former id → `308` to the current one. `Cache-Control: no-store`, no ETag: its `alerts` are live |
| `GET /api/v1/lines` | includes `mode`, `hidden`, `replaces`; ETag as stations. Hidden lines are included with the flag set |
| `GET /api/v1/lines/{id}` | + `directions` |
| `GET /api/v1/shapes` | every direction's path, `[lon, lat]` pairs; ETag = the shapes' own hash + schema hash |
| `GET /api/v1/arrivals?platforms=a,b&limit=6` | 1–50 platform ids; keyed by platform id (another stop of a platform answers under the platform's id); `refreshAfter` |
| `GET /api/v1/vehicles?line=a,b` | `line` optional; in-service vehicles only; `refreshAfter`; no per-vehicle time (511 stamps a feed with one) |
| `GET /api/v1/alerts?line=&station=&platforms=` | active now; `refreshAfter` |
| `GET /health` | `app.models.api.Health` |

Errors: `app.models.api.Problem` (`{error, message}`). `error` is one of `not-found`, `bad-request`, `unauthorized`, `conflict`, `unavailable`.

## Realtime rules

* Arrival time = `arrival.time`, else `departure.time` (`kind: "departure"`).
  About 1,000 updates per feed are departure-only, all at trips' first stops; an
  arrival-only scan misses 15 terminal stops entirely.
* `terminates` = this stop is the last `stop_time_update` of its trip. Most trips
  end at the real terminals; about 1 in 10 metro trips ended mid-line in the
  samples. Whether those are short turns or truncated updates is **unverified**:
  compare against GTFS `stop_times` before relying on it for anything but a label.
* Vehicle `bearing`/`speed` of `0.0` → `null`.
* No `trip` / no `route_id` → out of service, excluded.
* Each poll builds a new index and swaps it in whole. A failed poll keeps the last
  good one; `fetchedAt` tells the client how old it is.
* Raw payloads are persisted. On startup a feed is fetched only if its saved copy
  is older than its interval.
* No `API_511_KEY` → no polling at all. `FIXTURES=1` → replay
  `tests/fixtures/realtime/`.

## Editor and /map

State and requests: `app/models/editor.py`. `/editor/api/*` and all of `/editor`
require the session cookie; `/map` and `/map/api/state` are public. The frontend
is vanilla ES modules with no build step, taken from branch
`claude/beautiful-cerf-23u8yd` (`editor/public/`), and lives in `api/editor/`.
Its fetch paths are relative (`api/state`), so it works under either prefix.

**Remove:** levels, exits, `isIsland` and all their UI; pole dragging; editing
`stopName`; editing a platform's lines; editing `terminates`; the client-side
validator; the Node server, `lib/`, Dockerfile, entrypoint; the SFMTA zip "feed
check"; the name "Atlas".

**Change:** load `EditorState`; qualified ids; flat platforms; station coordinate
= centroid; the line strip shows `directions` read-only; save posts the whole
`Curation` with `baseVersion`; the change list is rewritten for the new model.

**Add:** login page; mode chips (Metro / Streetcar / Cableway / Bus, a station
matches if any of its lines does); line rail grouped by mode; verification
(unverified-only filter, progress count, next-unverified key, mark-verified key);
line editing (name, colour, mode, hidden, replaces); notes; review queue (wave 3);
**live vehicle layer**, off by default, toggled from the layer controls, drawn
from `/api/vehicles`, following the mode and line filters; `/map` read-only mode.

Editor endpoints (track F), all under `/editor/api/` and all requiring the session:

| endpoint | |
|---|---|
| `GET state` | `EditorState` |
| `POST validate` | `ValidateRequest` → `ValidateResponse` (validation + derived for the unsaved curation) |
| `POST save` | `SaveRequest` → `SaveResponse`; `409 conflict` if the rebase does not apply |
| `GET review` | `ReviewResponse`: unassigned stops with the proposer's suggestion, dead platforms, dead stations. Accepting a proposal is an ordinary curation edit + save |
| `POST snapshot/fetch` | two 511 calls (GTFS zip, lines); returns `SnapshotFetchResponse` with the drift report; commits nothing. 503 without a key, in fixtures mode, or with under 2 calls of budget left |
| `POST snapshot/commit` | commits the pending snapshot's `snapshot/<op>/` files through the save path |
| `GET history` | recent sf-transit commits touching `curation/`: `{commits: [{sha, subject, author, date, url}]}`. No diff endpoint: the URL is the diff |

Error bodies beyond `Problem` live in `app/editor/models.py`: a 422 save is
`InvalidSave` (`Problem` + `validation`), a 409 is `SaveConflict` (`Problem` +
the `version` to reload + the conflicting `paths`). Every POST must be
`Content-Type: application/json` (415 otherwise): with `SameSite=Strict`, that is
the CSRF defence. Login attempts are throttled by the **last** `X-Forwarded-For`
entry, the one our own proxy adds; the first is client-controlled. With no
`EDITOR_PASSWORD`, `GET state` and `GET history` are open (the data is public)
and every POST is refused.

There is no discard endpoint: unsaved edits live only in the browser, and a save
writes and commits in one step, so the checkout is never left dirty.

Save = write the changed curation files → `git pull --rebase` → commit (author
from env) → push. A rebase conflict is a 409 with the details, never resolved
automatically. An unchanged document makes no commit.

## Work plan

The contract (wave 0) is done first and reviewed. Later waves fan out to one agent
per track, each in its own git worktree on its own branch. Every track owns the
folders listed and **edits nothing outside them**. Shared files (`app/main.py`,
`pyproject.toml`, `Dockerfile`) are the integrator's; a track that needs a
dependency says so in its report instead of editing `pyproject.toml`.

| wave | track | owns | done when |
|---|---|---|---|
| 0 | contract | `app/models/`, `app/settings.py`, `tests/test_contract.py`, `tests/fixtures/`, this file | reviewed |
| 1 | **A** seed + static ingest | `scripts/seed.py` (one-off, applied as sf-transit's first commit, then retired), `app/ingest/gtfs_static.py`, `tests/ingest/` | every old platform appears exactly once or is reported with a reason; every station id kept except the rename; zero validation errors; 511 vs SFMTA GTFS difference explained |
| 1 | **B** server core | `app/data/`, `app/endpoints/{stations,lines}.py`, `tests/data/`, `tests/endpoints/` | loader + validator + endpoints pass against `tests/fixtures/transit` |
| 1 | **C** realtime | `app/upstream/`, `app/realtime/`, `app/db.py`, `app/endpoints/{arrivals,vehicles,alerts}.py`, `tests/realtime/` | fixtures replay; restart does not re-fetch fresh feeds; the budget ceiling holds under a simulated burst |
| 1 | **D** frontend removals | `editor/` | removals done; the frontend still loads |
| 1 | **G** proposer | `app/ingest/propose.py`, `tests/propose/` | drop 10% of assignments at random and it reproduces them; every mismatch explained |
| 2 | integrate + deploy | `app/main.py`, `app/endpoints/health.py`, `Dockerfile`, `pyproject.toml` | live on staging with real sf-transit data |
| 2 | **E** frontend on the new model | `editor/` | editor works against `EditorState` |
| 2 | **F** editor server | `app/editor/`, `app/endpoints/map.py`, `tests/editor/` | rename-a-station makes a one-line commit; an unchanged save makes none |
| 3 | **H+I** vehicle layer + `/map` | `editor/` | toggle works on both pages; `/map` shows no editing controls |
| 3 | **J** review queue + snapshot refresh | server: `app/editor/{review,refresh}.py` (done). UI: `editor/` review panel + refresh flow | unassigned stops can be assigned, created or ignored; a refresh shows its drift before committing |
| 4 | docs, freeze notes, Worker `/gtfs` poller off (needs explicit OK) | | |

## Interfaces between tracks

Tracks B and C are written at the same time and meet only here. Both read what
they need from `request.app.state`; the integrator (wave 2) builds these objects
in `app/main.py` and owns `/health`.

```python
app.state.settings : app.settings.Settings            # contract (done)

app.state.network  : app.data.network.Network          # track B
    .version: str                                      # sf-transit commit
    .station_of(stop_id: str) -> str | None            # owning station, if assigned
    .platform_of(stop_id: str) -> str | None           # owning platform's id, if assigned
    .stops_of(stop_id: str) -> list[str]               # its platform's stops, or just itself
    .headsign(line_id: str, direction: int) -> str | None   # most-run pattern's headsign
    .is_live(stop_id: str) -> bool                     # assigned AND in the snapshot

app.state.realtime : app.realtime.state.Realtime       # track C
    .arrivals(ids: list[str], limit: int) -> ArrivalsResponse   # merged per platform
    .vehicles(lines: set[str] | None) -> VehiclesResponse
    .alerts(*, lines=None, stations=None, platforms=None) -> AlertsResponse  # active now
    .health() -> tuple[dict[str, FeedHealth], BudgetHealth]
```

`Realtime` is constructed with `network: Callable[[], NetworkView]`, a function
returning the *current* network, because the network is replaced whole when the
editor saves. `NetworkView` is a `typing.Protocol` defined by track C in
`app/realtime/` with exactly `station_of` and `headsign`; `Network` satisfies it
without importing it. Every realtime method that depends on the time takes an
optional `now: int`, so tests can use the fixture feed's own clock.

Station detail (track B) fills `alerts` from `app.state.realtime.alerts(stations={id})`
when `app.state` has a `realtime`, and with `[]` otherwise.

## Rules for agents

* **Never call 511 and never read `api/.env`.** Tests and local runs use
  `FIXTURES=1` and the files under `tests/fixtures/`. The seed track reads the
  already-downloaded upstream files at the path it is given.
* Don't commit to `master`. Work on your track's branch in your worktree; the
  integrator reviews and merges.
* Match the existing style: comments explain *why*, not what, and name the
  evidence (a measurement, a real stop) behind a non-obvious choice.
* Report anything the contract got wrong instead of working around it.
