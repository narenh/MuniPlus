# Published data format, version 1 (draft)

What the Canopy Transit Editor publishes for Muni+ to download. Status: **draft
for review**. Nothing implements it yet.

## Principles

- **The editor is the provider.** The published files hold everything the app
  needs. The app never asks SFMTA or 511 for station, line, colour or shape data.
- **511 is an input, not a source.** A snapshot refresh produces proposed changes
  in the editor. Nothing reaches the published files until the user accepts it.
- **Download once per change.** Every data file is named by a hash of its
  contents and never changes. A small manifest says which files are current.
- **Metro and the F first.** Version 1 holds 9 lines and about 148 stations.
  Nothing in it assumes rail only, so buses can be added later as more data, not
  a new format.

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
  "files": {
    "network": { "path": "network.3f9a1c0be2d4a771.json", "sha256": "3f9a1c0b…", "bytes": 81234 },
    "shapes":  { "path": "shapes.a0c4e19d55b2f803.json",  "sha256": "a0c4e19d…", "bytes": 40211 }
  }
}
```

Paths are relative to the manifest. The app verifies each file's `sha256` before
using it.

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
- **Buses later:** they will add shared styles such as `bus-local` and
  `bus-rapid`, which many lines point to.

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
  "directions": [
    { "id": "inbound",  "headsign": "Embarcadero",  "platforms": ["15418", "…"] },
    { "id": "outbound", "headsign": "Balboa Park",  "platforms": ["…", "17778"] }
  ]
}
```

- **`directions[].platforms`** is the stop order, as platform ids, in travel
  order. It replaces the old single `stationIds` list. A loop or a one-way
  section is written out as it really runs.
- **Owl and replacement lines** (NOWL, KBUS, …): `"replaces": "N"`,
  `"drawn": "outlined"`, and the same style as the line they replace. The app
  keeps its convention without hard-coding it.
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
    { "id": "16992", "heading": "eastbound",
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
decimals. It's a separate file because it's the largest file and the one that
changes least.

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
