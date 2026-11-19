# Published data format, version 1 (draft)

What the Canopy Transit Editor publishes for Muni+ to download. Status: **draft
for review**. Implemented by `editor/transit_editor/publish/`:

```
python -m transit_editor.publish --data <static-data checkout> --out <dir>
```

builds the files from a data checkout (`curation/` + `snapshot/`) and writes them
to `<dir>/data/1/`. It refuses to write anything if the editor's validator reports
an error or the published files would not be self-consistent (see "Checks").

## Principles

- **The editor is the provider.** The published files hold everything the app
  needs. The app never asks SFMTA or 511 for station, line, colour or shape data.
- **511 is an input, not a source.** A snapshot refresh produces proposed changes
  in the editor. Nothing reaches the published files until the user accepts it.
- **Download once per change.** Every data file is named by a hash of its
  contents and never changes. A small manifest says which files are current.
- **Everything Muni runs.** Version 1 holds every line that isn't hidden in the
  editor (Metro, the F, cable cars, every bus, owls and replacement lines: 69 in
  November 2026) and every curated station that has a stop in 511's current data
  (1,789 of 1,791), bus-only ones included.

## Files and URLs

Served by Transit Static (`transit-static.canopysf.com`):

```
/data/1/manifest.json                   small, revalidated on each launch
/data/1/network.<hash>.json             lines, styles, stations, platforms
/data/1/shapes.<hash>.json              the path each line draws on a map
```

- `<hash>` is the first 16 hex characters of the file's SHA-256.
- Hashed files are served with `Cache-Control: public, max-age=31536000,
  immutable`.
- The manifest is served with `Cache-Control: no-cache` and an `ETag`, so most
  checks get a 304.
- `1` is the **format** version. A breaking change publishes `/data/2/` next to
  `/data/1/`, so installed apps keep reading the format they understand until
  `/data/1/` is retired.

### manifest.json

```json
{
  "format": 1,
  "published": "2026-11-20T18:04:00Z",
  "source": "4f1c2a9…",                  // the static-data commit it was built from
                                         // (null if the data isn't a git checkout)
  "files": {
    "network": { "path": "network.3f9a1c0be2d4a771.json", "sha256": "3f9a1c0b…", "bytes": 81234 },
    "shapes":  { "path": "shapes.a0c4e19d55b2f803.json",  "sha256": "a0c4e19d…", "bytes": 40211 }
  }
}
```

Paths are relative to the manifest. The app verifies each file's `sha256` before
using it.

`published` is the source commit's commit time (UTC), not the clock, so
publishing the same commit twice writes the same manifest byte for byte. The
manifest is pretty-printed; the hashed files are compact (no whitespace), UTF-8,
with keys in the order this document lists them.

## network.json

```json
{
  "format": 1,
  "agency": { "id": "SF", "name": "Muni" },
  "styles":  { … },
  "lines":   [ … ],
  "stations": [ … ],
  "subways":  [ … ]
}
```

### styles: colours, owned by the editor

The palette settled in the November 2026 colour audit moves here from
`Line.swift`. A colour is always a light and dark pair, as `#RRGGBB`.

```json
"styles": {
  "J": { "fill": { "light": "#FAA633", "dark": "#FAA633" },
         "text": { "light": "#FFFFFF", "dark": "#FFFFFF" },
         "textEdge": { "light": "#C98A00", "dark": "#C98A00" } },
  "N": { "fill": { "light": "#00529C", "dark": "#266BBC" },
         "text": { "light": "#FFFFFF", "dark": "#FFFFFF" } },
  "F": { "fill": { "light": "#9999FF", "dark": "#9999FF" },
         "text": { "light": "#FFFFFF", "dark": "#FFFFFF" } }
}
```

- **`textEdge`** is optional: the thin edge around the badge letter (J and S).
  How thick it's drawn is the app's business.
- **Style ids.** A line with a curated style (the Metro lines, the F, the S) uses
  the palette's, under the line's id. Every other line (buses, cable cars) has a
  style of its own under its line id, made from 511's route colour and text
  colour, the same value for light and dark. Only styles some line uses are
  published. Moving buses to shared styles such as `bus-local` and `bus-rapid`
  later is a change to the style id a line points to, not to the format
  (`_own_style` in `publish/build.py`).

### lines

```json
{
  "id": "J",
  "name": "J Church",
  "badge": "J",
  "mode": "lightRail",                  // lightRail | streetcar | bus | cableCar
  "style": "J",
  "drawn": "solid",                     // solid | outlined
  "replaces": null,                     // set on owl and bus-replacement lines
  "listedAtStations": true,             // false: never shown on stations (the S)
  "owl": false,                         // overnight service (NOWL, LOWL, 90, 91)
  "directions": [
    { "id": "0", "headsign": "Balboa Park",          "platforms": ["17217", "…"] },
    { "id": "1", "headsign": "Embarcadero Station",  "platforms": ["17778", "…"] }
  ]
}
```

- **`name`** is the curated name, else `"<short name> <511 long name in title
  case>"` (`"14R Mission Rapid"`, `"NOWL Owl Judah"`). **`badge`** is 511's short
  name (`J`, `14R`, `NOWL`).
- **Order.** Lines are listed rail first (Metro, the F, cable cars, then buses),
  numbers in numeric order (5 < 5R < 38 < 38R) before lettered lines. A
  platform's `lines` use the same order.
- **`directions[].id`** is GTFS `direction_id` as a string, `"0"` or `"1"`, the
  same value GTFS-rt uses. For SFMTA, `0` is usually outbound and `1` inbound,
  but not for every line (the T's `0` runs to Chinatown, the 39's to Coit Tower),
  so the format doesn't claim it. A line may have only one direction (the 30X).
  A hand-entered line's first direction is `"0"`. Each direction is the most-run
  511 pattern in that direction (or the hand-entered stop order). A stop that no
  station has is skipped, and two consecutive stops on one platform appear once.
- **`owl`**: 511 names the line an Owl. With `replaces`, it tells an owl that
  covers a line overnight (NOWL) from a bus standing in for it by day (KBUS),
  for the app's OWL BUS label. An owl with nothing to replace (90, 91) is drawn
  `solid` in its own style.

- **`directions[].platforms`** is the stop order, as platform ids, in travel
  order. It replaces the old single `stationIds` list. A loop or a one-way
  section is written out as it really runs.
- **Owl and replacement lines** (NOWL, KBUS, …): `"replaces": "N"`,
  `"drawn": "outlined"`, and the same style as the line they replace. The app
  keeps its convention without hard-coding it. The editor stores `replaces` as a
  list; the first entry is published (no line has more than one today). Every
  other line is `"drawn": "solid"`, `"replaces": null`.
- **Hidden lines** (hidden in the editor) are not published at all, and no
  platform lists them.
- **Ids** are the line's public name (`J`, `NOWL`, `KBUS`). There's no `SF:`
  prefix, because the agency is given once at the top. They're the same as
  511's `LineRef`.
- **`listedAtStations: false`** keeps a line off every station and platform
  `lines` list, so it never appears as a station badge or on a transfer chip.
  Its `directions` are still published. The S Shuttle is the case today: it runs
  weekdays through the Market Street Subway, but stations shouldn't list it.
- **Predictions are coloured from `lines`, not from stations.** For each
  predicted train, the app finds the line whose `id` equals the `LineRef` and
  draws it with that line's style. A train whose `LineRef` matches no line is
  dropped, as today. So a line that no station lists, like the S, still shows up
  and in the right colours.
- **Hand-entered directions.** Most lines' directions start from 511's trip
  patterns. A line 511 doesn't publish, like the S (it isn't in 511's GTFS), has
  its stop order entered by hand in the editor, from SFMTA's route page.

### stations

```json
{
  "id": "embarcadero",
  "name": "Embarcadero",
  "kind": "underground",                // underground | streetLevel
  "coordinate": { "lat": 37.792862, "lon": -122.39685 },
  "formerIds": [],
  "agencies": ["bart"],                 // bart | caltrain
  "transfers": [ { "station": "marketEmbarcaderoSubway", "mode": "street" } ],
  "platforms": [
    { "id": "16992", "stops": ["16992"], "formerIds": [],
      "heading": "eastbound",
      "coordinate": { "lat": 37.793134, "lon": -122.396431 },
      "lines": ["J", "K", "L", "M", "N"],
      "label": null,
      "terminus": false }
  ]
}
```

- **Platform `id`** is the SFMTA stop code: what predictions are fetched by, and
  **permanent**. Favourites and homes store it, not the platform's position in
  the list as today.
- **Platform `stops`** is every stop code at the platform that 511 currently
  publishes, the `id` first. Usually just the `id`; where 511 numbers one place
  twice (the N and its bus substitute at Duboce & Church, 14448 and 18061), the
  app fetches predictions for each and shows them together. A direction lists
  the platform's `id`, whichever of its stops 511's pattern names.
- **Platform `formerIds`** are stop codes the platform had before (a stop 511
  retired or renumbered). A saved platform id not found as an `id` is looked up
  here.
- **`kind`** is `underground` for a station in a subway's `stations` list,
  otherwise `streetLevel`.
- **`coordinate`** of a station is the centroid of its platforms' stops; of a
  platform, the centroid of its stops. Six decimal places.
- **`agencies`** are the other operators reachable there, from the editor's
  transfer agencies: `bart`, `caltrain`. Any other 511 operator code would be
  published lower-cased (none today).
- **`transfers`** are the walks to other stations, listed on both stations, in
  order of the other station's name. Only to published stations.
- **Order.** Stations are listed by id; platforms in the curated order.
- A curated station with no stop in 511's current data has no coordinate and is
  left out until it has one (the validator warns about it).
- **`formerIds`** lists retired station ids. The app uses them to move saved
  favourites and homes onto the new station, for example `mongomery` →
  `montgomery`.
- **`terminus`** replaces putting "(Terminus)" in the platform's name.
- **`label`** is the platform's own text, such as "To Castro via Wharf Loop". It
  no longer carries the terminus marker.
- **`lines`** on a platform is which lines stop there. The station's lines are
  the union of its platforms' lines, so they aren't stored twice.
- **Coordinates** are stored, edited and owned by the editor. They're no longer
  looked up from the 511 snapshot.

### subways

```json
{ "id": "marketStreetSubway", "name": "Market Subway", "stations": ["embarcadero", "…"] }
```

## shapes.json

```json
{
  "format": 1,
  "lines": {
    "J": { "inbound": [[37.7206, -122.4467], …], "outbound": [ … ] }
  }
}
```

The path each direction of each line draws, as `[lat, lon]` pairs with 6
decimals, keyed by line id and then direction id (`"0"`, `"1"`). It's a
separate file because it's the largest file and the one that changes least.

- The path is 511's shape for the direction's most-run pattern, with the
  editor's curated corrections spliced in, cut to the first and last stops
  riders use, and simplified to within half a metre.
- A direction with no shape is absent: a hand-entered line (the S) has none.
  The app draws it through its platforms' coordinates, or not at all.

## Checks

Before writing, the publisher runs the editor's validator (any error stops it;
warnings are printed) and then checks the files themselves:

- no duplicate line, station, platform or subway id, or direction id in a line;
- no former station or platform id that is a current id or claimed twice;
- every station has at least one platform;
- every platform a direction lists, every transfer's station, every subway
  station, every line a platform lists and every style a line uses exists;
- `replaces` names a published line, and no platform lists a line with
  `listedAtStations: false`;
- every direction has at least one platform, and every shape belongs to a
  published line direction and has two or more points;
- each file reads back from JSON to the same value.

## What the app does

1. **Bundled copy.** It ships a copy of the manifest and both files in the
   bundle, as the floor for a first launch with no network (as `data.json` is
   today).
2. **Check on launch.** On launch it `GET`s `manifest.json` with
   `If-None-Match`. A 304 means nothing to do.
3. **Download what changed.** For each file whose `sha256` differs from the
   cached one, it downloads that file and checks its hash. Only then does it
   cache it.
4. **Apply next launch.** A change applies on the next launch, never
   mid-session, as the catalog and the map work today.
5. **Never download the same file twice.** A file whose hash it already has is
   never downloaded again.
6. **Unknown format.** A manifest with a `format` the app doesn't know is
   ignored, and the app keeps what it has.

## Saved favourites and homes: migration

**Today (3.0.1 – 3.0.4):**
- A favourite or the home is saved in UserDefaults as `{stationId,
  platformIndex}`: a station id plus the platform's **position** in that
  station's list.
- On every launch, `FavoritesManager.pruneRetiredStops()` **permanently deletes**
  any favourite, and clears the home, whose station id isn't in the catalog or
  whose position is out of range.
- So with those versions:
  - renaming or merging a station deletes its favourites;
  - reordering a station's platforms silently points favourites at a different
    platform;
  - removing a platform deletes favourites at the last positions.

**From the release that reads this format, the app:**

1. **Saves platform ids.** It saves `{stationId, platformId}`. On first launch of
   that release, each old `{stationId, platformIndex}` is converted using the
   catalog it was saved against: the legacy `data.json` the app already has
   cached, or else the bundled copy. A favourite whose position can't be resolved
   keeps the station and drops the platform.
2. **Moves old ids before checking anything.**
   - For each saved station id that isn't in `stations`, it looks for the
     station whose `formerIds` contains it and rewrites the saved id.
   - A platform id that isn't in the station is looked up the same way, for
     example where a merge moved it to another station.
   - This runs before any check that a favourite resolves, and saves its result.
3. **Never deletes a favourite.** One that still doesn't resolve is kept,
   hidden, and shown again if a later catalog has its station. A bad or partial
   download can't destroy saved data.
4. **Bundles only after the code.** A release must not bundle a catalog with
   renamed ids unless it also contains steps 1–3, because the bundled copy is
   what a first launch checks against.

**The editor guarantees:**
- **`formerIds` are permanent.** Once a station id is retired, it stays in
  `formerIds` for good.
- **No reuse.** No current id equals any station's former id.
- **Platform ids are stop codes and are never reused.** A platform keeps its
  id; when 511 renumbers it, the old id goes into its own `formerIds`.

## What the editor does

- **Storage.** Its stored data becomes this shape, as a complete set of
  stations, lines, styles and shapes. It stops being the minimal curation plus
  values worked out from the snapshot.
- **Review.** The 511 snapshot is kept only to compare against. A refresh lists
  proposed changes (a stop moved 12 m, a pattern changed), each accepted or
  rejected by hand.
- **Publishing:**
  - checks the data;
  - writes `network.json` and `shapes.json` in a stable key order, so the same
    data always gives the same hash;
  - names the files by hash and writes a new manifest;
  - commits them to the `static` branch, under `public/data/1/`.
- **Legacy output.** For installed 3.0.x copies it also writes the legacy
  `public/data.json`. This is built **from the published `network.json` only**,
  by a separate module, so deleting that module when the old domains retire
  removes it completely.
- **The legacy output is frozen in shape.** Because 3.0.x deletes favourites it
  can't resolve, the legacy exporter must never change, for any station in the
  current legacy file:
  - its id;
  - its platform list: count, order and ids.

  It keeps a frozen map of legacy station id → platform ids in order, taken from
  today's `data.json`. Merged stations are split back to their legacy ids, and
  renamed ones keep their old id (`mongomery`). Headings, labels, termini, lines,
  names and coordinates may change.

  The exporter **refuses to publish** if a legacy station or platform can't be
  produced. It never quietly drops one.

## Open questions

1. **Merged stations.** The editor's data merges stations the app keeps apart:
   4th & King (`fourthKingN` + `fourthKingT`), Balboa Park (+ `sanJoseGeneva`),
   Duboce & Church (+ `churchDuboceJ`), and Market & Castro (+ `castroPlaza`).
   The merged 4th & King has three northbound platforms, which the current
   two-column tile layout can't show. Should version 1 publish the merges, with
   the app handling more than one platform per direction, or split them for now?
   The legacy `data.json` has to split them either way.
2. **Deploying on publish.** Committing to `static` doesn't deploy the
   `transit-static` Worker; today that's `wrangler deploy` from a Mac. The
   options:
   - connect the Worker to Cloudflare's build-on-push for the `static` branch
     (set up once in the dashboard);
   - give the editor a Cloudflare token scoped to that one Worker;
   - move the files to R2 and have the Worker serve from there.
3. ~~**Porting fixes.**~~ Done 16 November 2026: the direction fixes (f94b4da) and
   termini (d2c334b, using the new `terminus` flag) are in the editor's data.
