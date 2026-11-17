# Canopy Transit Editor — design

What the editor edits and the rules it keeps. The types in
`transit_editor/models/` are the contract; where this document and the code
disagree, the code is right and this document needs fixing.

Ported from the retired v1 API's `api/PLAN.md` (in git history on `master`),
less the parts about the app-facing API and the realtime feeds, which went with
it.

## Terms

* **stop**: one place a line stops, as 511 numbers it (`SF:16992`). What 511 and
  the realtime feeds speak in. Type `StopId`.
* **platform**: one place a rider stands. Usually one stop; where 511 numbers one
  place more than once (the N and its bus substitute on one Duboce shelter), several.
  Its id is its primary stop's; `stops` lists the others. Read a platform's stops
  through `Platform.all_stops` (JS `stopsOf`), never `id` alone.
* **station**: the platforms riders think of as one place, with a name and an id of
  our own.

## Decisions

| topic | decision |
|---|---|
| stack | Python 3.13, FastAPI, Pydantic v2. **One uvicorn worker**: saves are serialised in-process and a pending snapshot is on local disk |
| deploy | Coolify builds `master` with base directory `/editor`, persistent volume at `/data` |
| data | branch `static-data` of this repo; the editor commits there with a deploy key; nothing redeploys on a data edit |
| 511 | never called directly. A snapshot refresh downloads 511's files through the Canopy Transit Relay, which holds the key |
| vocabulary | **lines**, never routes, everywhere (511 says lines). GTFS "routes" are renamed at ingest. HTTP handler modules live in `transit_editor/endpoints/` and `transit_editor/editor/` |
| ids | `<511 operator>:<upstream id>` for stops and lines (`SF:16992`, `SF:LOWL`), no mapping table. A platform's id is its primary stop's. Station ids are ours, `[A-Za-z0-9]+`, permanent |
| renames | `mongomery` → `montgomery`; the old id lives in `formerIds` and is flagged as a client favourites migration |
| stations | one flat platform list; a platform may take in several stops (`stops`), curated by hand, never by distance. Levels/exits come later as an optional `layout` field. All hand-curated station info is in `curation/stations.json` |
| platform identity | the app keeps **platform** ids (the primary stop's) as homes and favourites, so they must last. A stored id is found by a platform's `id`, then its `stops`, then its `formerIds`. The editor records a former id whenever a platform survives losing a stop id: removing a dead primary, or the review queue's "Replaces" (a 511 renumbering, e.g. 18165 for 13244 at 11th & Market). Only a deleted platform's id is truly gone |
| transfers | always both ways: one entry per pair of stations, in `stations.json`'s top-level `transfers` (`{between: [a, b], mode}`), so a one-way transfer cannot be written. Only the 12 pairs from the iOS app's data were kept; the generator's one-way links were dropped (2026-09-23) |
| coordinates | never curated. Stops take 511's; a platform is the centroid of its live stops, a station of all its live stops |
| modes | 511's line `TransportMode` verbatim (`metro`, `bus`, `cableway`). One curated override: the F is `streetcar` |
| replacements | `replaces` on a line (`SF:LOWL` replaces `SF:L`). **No** replacement platforms, no station-level mapping |
| terminates | **not curated.** A realtime client derives it per trip: departure-only = the trip starts here; arrival-only last stop = the trip ends here |
| line diagrams | the most-run 511 pattern per direction, read-only in the editor; no manual reordering |
| new 511 stops | not in the derived data until assigned to a station (or ignored) in the editor's review queue |
| validation | one Python validator, run by the server for the loader and the editor; no copy in the browser |
| hubs | `hub: true` on a station marks a major metro interchange (4th & King, Union Square, Balboa Park). Curated, because a rule on line counts also catches every stop where two branches meet |
| verification | `verified` date per station. The seed marks hand-curated stations verified and generated bus stations unverified. A change to a station's platform list clears it |

## Data (branch `static-data`)

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

Models: `transit_editor/models/curation.py`, `transit_editor/models/snapshot.py`. Reading and writing:
`transit_editor/models/files.py`, which is **the only code that writes these files**. Its
format is deterministic (sorted maps, unset fields omitted, two-space indent), so
a round trip is byte-identical and an unchanged save writes nothing.

A snapshot refresh for SF is two files from 511, the GTFS zip (`/transit/datafeeds`)
and `/transit/lines` (for `mode`, which the zip lacks), downloaded through the
Canopy Transit Relay (`/static/datafeeds`, `/static/lines`), which holds the 511
key and keeps each for a day. Shapes come from the same
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
* `platform-former-id-collides`: a platform's former id is a current stop, or another platform's former id
* `station-has-no-platforms`

Warnings, which never block:

* `stop-not-in-snapshot` (dropped from the derived station and line data)
* `platform-stops-far-apart` (a platform's extra stop more than 25 m from its primary:
  two ids for one shelter sit metres apart)
* `station-has-no-live-platforms` (left out of the station list `Network` builds; still in `derived`)
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
applying after a refresh renumbers the shapes. They are applied before `api/shapes`
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

## Editor and /map

State and requests: `transit_editor/models/editor.py`. `/editor/api/*` and all of `/editor`
require the session cookie; `/map` and `/map/api/state` are public. The frontend
is vanilla ES modules with no build step, taken from branch
`claude/beautiful-cerf-23u8yd` (`editor/public/`), and lives in `editor/frontend/`.
Its fetch paths are relative (`api/state`), so it works under either prefix.

**Remove:** levels, exits, `isIsland` and all their UI; pole dragging; editing
`stopName`; editing a platform's lines; editing `terminates`; the client-side
validator; the Node server, `lib/`, Dockerfile, entrypoint; the SFMTA zip "feed
check"; the old product name.

**Change:** load `EditorState`; qualified ids; flat platforms; station coordinate
= centroid; the line strip shows `directions` read-only; save posts the whole
`Curation` with `baseVersion`; the change list is rewritten for the new model.

**Add:** login page; mode chips (Metro / Streetcar / Cableway / Bus, a station
matches if any of its lines does); line rail grouped by mode; verification
(unverified-only filter, progress count, next-unverified key, mark-verified key);
line editing (name, colour, mode, hidden, replaces); notes; review queue;
`/map` read-only mode. (The live vehicle layer went with the v1 API's realtime
feeds, November 2026.)

Editor endpoints, all under `/editor/api/` and all requiring the session:

| endpoint | |
|---|---|
| `GET state` | `EditorState` |
| `POST validate` | `ValidateRequest` → `ValidateResponse` (validation + derived for the unsaved curation) |
| `POST save` | `SaveRequest` → `SaveResponse`; `409 conflict` if the rebase does not apply |
| `GET review` | `ReviewResponse`: unassigned stops with the proposer's suggestion, dead platforms, dead stations. Accepting a proposal is an ordinary curation edit + save |
| `POST snapshot/fetch` | downloads the GTFS zip and lines from the relay; returns `SnapshotFetchResponse` with the drift report; commits nothing. 503 when the relay has no copy or is unreachable |
| `POST snapshot/commit` | commits the pending snapshot's `snapshot/<op>/` files through the save path |
| `GET history` | recent data branch commits touching `curation/`: `{commits: [{sha, subject, author, date, url}]}`. No diff endpoint: the URL is the diff |

Error bodies beyond `Problem` live in `transit_editor/editor/models.py`: a 422 save is
`InvalidSave` (`Problem` + `validation`), a 409 is `SaveConflict` (`Problem` +
the `version` to reload + the conflicting `paths`). Every POST must be
`Content-Type: application/json` (415 otherwise): with `SameSite=Strict`, that is
the CSRF defence. Login attempts are throttled by the **last** `X-Forwarded-For`
entry, the one our own proxy adds; the first is client-controlled. With no
`EDITOR_PASSWORD`, `GET state` and `GET history` are open (the data is public)
and every POST is refused.

Public, under both `/editor/api/` and `/map/api/`: `GET shapes` (`ShapesResponse`,
every line direction's path; ETag is the shapes' own hash). `/map/api/state` is
`EditorState` with `readOnly` set and no validation or repo status. `GET /health`
says whether the data is loaded.

There is no discard endpoint: unsaved edits live only in the browser, and a save
writes and commits in one step, so the checkout is never left dirty.

Save = write the changed curation files → `git pull --rebase` → commit (author
from env) → push. A rebase conflict is a 409 with the details, never resolved
automatically. An unchanged document makes no commit.

