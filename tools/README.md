# Surface transit data

`appdata/data.json` covers Muni Metro. This directory generates everything else —
58 bus routes and 3 cable car lines — into **one station namespace shared with the
metro file**, so a stop served by a train and a bus is a single station.

| file | what it is |
|---|---|
| `appdata/data.json` | Muni Metro. Hand-curated, **authoritative, never written by the generator** |
| `appdata/bus.json` | all 61 surface routes merged — the file the app loads |
| `appdata/bus/<route>.json` | one file per route. Source of truth for readable diffs; the app does not need these |
| `tools/build_bus_data.py` | the generator |
| `tools/station-ids.json` | frozen stop code → station id map |

Merged with `data.json`: **1,816 stations, 3,229 platforms, 68 lines.**
`bus.json` is 1.17 MB (99 kB gzipped).

## Loading it

Parse `data.json` first, then fold in `bus.json`, keyed by station `id`:

```
for each station S in bus.json:
    if S.id is not already loaded:  insert S
    else:
        existing.platforms += S.platforms whose id is not already present
        existing.lines     += S.lines not already present
        # keep the metro record's name, kind, latitude, longitude,
        # transferStations and transferAgencies — data.json wins
```

`lines` is the union of both files' `lines` arrays. Line ids do not collide.

That is the whole integration. There is no ordering requirement beyond loading
`data.json` first, and no route is optional — everything ships and everything shows.

### Why a station appears in both files

`churchMarket` is listed in `data.json` with platforms `17073`/`18059`, and in
`bus/22.json` with *the same two stop codes* — Muni gives the J's surface stop and
the 22's stop one pair of codes. That is why the J station already returns 22
predictions. `church16` overlaps on one pole only (`13984`), so the merge gives it
three platforms: the J's northbound, the 22's northbound, and the shared southbound.

## What the fields mean

* **`platforms[].id`** is the 5-digit public SFMTA stop code — the same value the
  prediction API already takes. It is unique across the whole merged namespace:
  no code appears under two stations, and no code has two headings.
* **`platforms[].heading`** is one of `northbound` / `southbound` / `eastbound` /
  `westbound`, and describes the street the stop *sits on* — see "How headings are
  decided" below. **Do not assume a station's platforms are an opposite pair.** After
  merging, stations have 1–7 platforms (`data.json` alone maxes out at 2):

  | platforms | stations |
  |---|---|
  | 1 | 788 |
  | 2 | 803 |
  | 3 | 96 |
  | 4 | 109 |
  | 5+ | 20 |

  `judah46` has four — the N runs east/west on Judah, the 18 runs north/south on
  46th Ave. Single-platform stations are one-way streets and terminals, not errors.
* **`kind`** is `underground` or `streetLevel`. Every generated station is
  `streetLevel`; only `data.json` has `underground` ones.
* **`transferStations`** links a surface station to a nearby metro station it was
  deliberately *not* merged into — `marketPowell` → `powell`. These links are
  **one-way (surface → metro)**, because writing the reciprocal would mean editing
  `data.json`. Symmetrize at load time if you want them bidirectional.
* **`subways`** is empty in `bus.json`; only `data.json` populates it.
* **`color`** for metro lines comes from `data.json` (hand-set, correct Muni
  branding). Surface colors come from GTFS: `#005B95` local, `#BF2B45` rapid,
  `#666666` owl, `#B49A36` cable car, plus a few route-specific ones.

## How station identity is decided

`data.json` is authoritative. For each intersection, in order:

1. An id already in `tools/station-ids.json` wins. **Ids are frozen on first mint
   and never change** — a shipped id lives in people's favourites, and a feed
   update must not rename a station someone has saved.
2. A stop sharing a stop code with a metro platform is filed under that station.
3. A stop at a street-level metro station joins it. **Underground stations are
   never merged into** — `data.json` models a surface stop next to an underground
   one as a separate station reached by `transferStations` (`church` vs
   `churchMarket`), and that convention is preserved.
4. Otherwise a new camelCase id is minted in `data.json`'s style (`churchMarket`,
   `church24`, `fourthKing`), never reusing one that already means somewhere else.

Two subtleties that were wrong before they were right, and will bite anyone
regenerating from a newer feed:

* **`data.json` decides how finely an intersection splits, not just what it is
  called.** It models Church & Duboce as two stations — `churchDuboceJ` on Church,
  `duboceChurchN` on Duboce. A cluster straddling both is split the same way, by
  which street the stop is named for first. Without this the 22's Church St stops
  get filed under the N's Duboce St station.
* **Curated headings win, and they are sometimes line-relative rather than
  geometric.** `dolores30` is labelled southbound although the J physically runs
  *east* there — its two platforms are 38 m apart east-west. `data.json` wins on
  every stop it describes.

## How headings are decided

Direction of travel alone is not enough. A bus stopped at `16th St & Church St` is
still running **west on 16th**; it only turns onto Church after leaving. Taking the
vector through the stop labels it northbound, which is wrong.

So a heading is the travel direction **snapped to the axis of the street the stop is
named for**. SFMTA names a stop `<street it is on> & <cross street>`, and the street's
local run comes from the nearest stops *named for that same street* within 600 m.
This corrects 157 of 3,092 stops (5%).

Two traps, both of which produced confidently wrong answers before they were fixed:

* **Only stops named FOR a street reveal how it runs.** `Market St & 3rd St` is a
  stop on Market. Counting it as a 3rd St stop drags 3rd's axis east-west and
  flips every 3rd St heading downtown.
* **SoMa, Mission Bay and South Beach sit on a grid rotated about 45°.** Consecutive
  3rd St stops there differ by `dx=245, dy=-240`; north versus west is a coin flip
  that rounding decides. When a street's run is not lopsided by at least 2:1 the
  snap is skipped and the travel direction stands, because neither answer is more
  right than the other.

### Where data.json disagrees, and why it still wins

19 of the 88 stops shared with `data.json` would get a different heading from the
rule above. They are two distinct things, and neither is a bug to fix silently:

* **A deliberate line-relative convention (11 stops).** The K is labelled east/west
  in the Market subway and north/south for its *entire* outer branch, Ocean Ave
  included — even though Ocean Ave runs east-west. Same for the J at `dolores30`.
  Consistent along the line, so leave it.
* **One pole, two lines, genuinely different directions (8 stops).** `13599` is
  `46th Ave & Taraval St`: the L turns there and is labelled eastbound, while the
  18 runs north-south on 46th Ave through the same pole. Both are right for their
  own line. The schema has one heading per platform and cannot express this. The
  others are `13600`/`13601`/`13602`/`13603` on 46th Ave, `16932` at the Zoo, and
  `17999` on 9th Ave. Deciding this needs a product call, not a code change.

## Regenerating

```sh
python3 tools/build_bus_data.py                   # fetches the current SFMTA feed
python3 tools/build_bus_data.py --gtfs local.zip  # or use a local copy
```

Source: <https://muni-gtfs.apps.sfmta.com/data/muni_gtfs-current.zip> (linked from
sfmta.com's GTFS page; the older `gtfs.sfmta.com` host no longer resolves).

Output is deterministic — re-running is byte-identical. The build fails on a stop
code under two station ids, a station id at two locations, a stop code with two
headings, or a `stationId` that resolves to nothing. A failure means the feed
changed in a way that needs a human decision, not a retry.

## Known warts

* `bus.json` and `bus/` also contain the 3 cable car lines. The paths predate
  them; renaming to `surface/` is a one-line change plus the path in the app.
* Owl and substitution routes (`NBUS`, `NOWL`, `KBUS`, `LOWL`, `FBUS`, `TBUS`)
  are included and trace the metro lines stop-for-stop. They are the main source
  of metro/surface station overlap. Delete those files and rebuild if they are
  noise in the UI; frozen ids make that safe.
* Station names keep SFMTA's street order, picked by majority vote across the
  stops at that intersection — so `gearyFillmore` is "Geary & Fillmore", not
  "Fillmore & Geary".
