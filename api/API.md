# Muni+ API v1

The reference for building a client, in particular the Swift SDK the Muni+ iOS app
will use. It covers every public endpoint, every field, what each one means and
how it behaves over time, and the rules a client must follow to keep working as the
data and the server evolve.

The response models are defined once, in [`app/models/api.py`](app/models/api.py),
and served as OpenAPI at `GET /api/openapi.json`. Treat that schema as the
authoritative field list, and this document as what the fields *mean*.

- **Staging:** `https://muni-staging.canopysf.com`
- **Production:** not yet decided. The base URL must be configurable.

## Contents

1. [Conventions](#1-conventions)
2. [The model: stations, platforms, stops, lines](#2-the-model)
3. [Identifiers, and which ones may be stored](#3-identifiers)
4. [Endpoints](#4-endpoints)
5. [Realtime: arrivals, vehicles, alerts](#5-realtime-semantics)
6. [Caching, versions and polling](#6-caching-versions-and-polling)
7. [Rules every client must follow](#7-rules-every-client-must-follow)
8. [Migrating the App Store app's saved data](#8-migrating-the-app-store-apps-saved-data)
9. [A suggested shape for the Swift SDK](#9-a-suggested-shape-for-the-swift-sdk)

---

## 1. Conventions

| | |
|---|---|
| **Paths** | Everything public is under `/api/v1/`. `/editor/*`, `/map/*` and `/health` are not part of the API. |
| **Method** | `GET` only. No authentication. |
| **Format** | JSON, UTF-8, keys in **camelCase**. |
| **Compression** | gzip when the request sends `Accept-Encoding: gzip`, which `URLSession` always does. The station list is 604 KB raw and 71 KB compressed. |
| **Times** | Unix epoch **seconds** (UTC), as integers. |
| **Coordinates** | WGS 84 decimal degrees, in fields named `lat` and `lon`. **The one exception is `/api/v1/shapes`**, whose points are GeoJSON `[lon, lat]` pairs. |
| **Nulls** | A nullable field is always present, with `null` when it has no value. A list is always present, and may be empty. |
| **Errors** | Every non-2xx response has a `Problem` body: `{"error": "<code>", "message": "<for a person>"}`. See [Errors](#errors). |

---

## 2. The model

A **stop** is one place a line stops, as 511 numbers it (`SF:16992`). 511 knows
nothing larger.

A **platform** is one place a rider stands: a pole, a shelter, a subway platform.
It is usually exactly one stop. Where 511 numbers one place more than once, one
platform holds all of those stops. For example, the N and its bus substitute share a
shelter at Duboce & Church as `SF:14448` and `SF:18061`. A platform has a
**heading** (the direction a rider there travels) and serves every line that stops
at any of its stops.

A **station** is the set of platforms a rider thinks of as one place: Embarcadero,
or Church & Market. Stations, their names, and how stops group into platforms are
hand-curated. Everything else comes from 511.

A **line** is a route as 511 lists it (J, 38R, LOWL). It has a **mode**, a colour
and up to two **directions**. Each direction has a headsign and an ordered list of
the stations and stops it serves.

**The app is built around platforms.** Someone's home and favourites are platforms,
because people mostly ride one direction from a given station. See
[section 3](#3-identifiers) for how to store them so that they keep working.

Other concepts:
- **Transfer:** a walk between two stations, always usable both ways. It is either
  `indoor` (a passage between the two) or `street`.
- **Subway:** a named, ordered group of underground stations (the Market Street
  subway).
- **Operator:** a transit agency, by its 511 code. `SF` is Muni and `BA` is BART.
  Today only Muni has platforms in the data. A station reports every operator that
  serves it in `operators`.

---

## 3. Identifiers

Every id is an **opaque string**. Never parse one, build one, or compare parts of
one. Pass back exactly what the API gave you.

| id | example | what it is | stable? | store it? |
|---|---|---|---|---|
| station | `embarcadero`, `500Parnassus` | our own id | **permanent** | **yes** |
| platform | `SF:16992` | the platform's primary stop | **resolvable**, see below | **yes** |
| stop | `SF:18061` | a 511 stop | changes only when 511 renumbers | no: store its platform |
| line | `SF:J`, `SF:38R` | a 511 line | stable in practice | yes, for filters |
| shape | `SF:9717` | a drawn path | changes with 511's data | no |
| trip, vehicle | `SF:12134484_M11`, `SF:2019` | 511's realtime ids | for minutes only | **never** |
| operator | `SF`, `BA` | a 511 operator code | stable | yes |

**Stations.** Station ids are permanent. When two stations are merged, or a station
is renamed, the id that went away is listed in `/stations`'s `formerIds` map (old
id to current id), and `GET /api/v1/stations/{old}` answers with a `308` redirect
to the current id. `URLSession` follows redirects on its own.

**Platforms.** A platform's `id` is its primary stop's id. Editing can change which
stop that is, so a stored platform id must be **resolved** against the latest
`/stations`, never matched against `id` alone. To find the platform a stored id
means, check each platform in this order:

1. its `id`;
2. its `stops` (the platform was merged into another one, and the stored id lives on
   as one of that platform's stops);
3. its `formerIds` (511 retired or renumbered the stop, and the platform took a new
   id).

The first match is the platform, and it may now belong to a different station. With
no match, the platform no longer exists: tell the person, and offer the platform
with the same `heading` at the station it used to belong to, if you know that
station.

The arrivals and alerts endpoints do this resolution themselves: ask with any of
the three and the answer is the current platform's. The client should still resolve
ids first, to know which key to read in the answer (see
[arrivals](#get-apiv1arrivals)).

---

## 4. Endpoints

| endpoint | what it returns | caching |
|---|---|---|
| [`GET /api/v1/stations`](#get-apiv1stations) | every station, with its platforms | ETag, `no-cache` |
| [`GET /api/v1/stations/{id}`](#get-apiv1stationsid) | one station in full, with its current alerts | `no-store` |
| [`GET /api/v1/lines`](#get-apiv1lines) | every line | ETag, `no-cache` |
| [`GET /api/v1/lines/{id}`](#get-apiv1linesid) | one line's directions | ETag, `no-cache` |
| [`GET /api/v1/shapes`](#get-apiv1shapes) | the path each direction draws | ETag, `no-cache` |
| [`GET /api/v1/arrivals`](#get-apiv1arrivals) | the next arrivals at some platforms | `no-store`, `refreshAfter` |
| [`GET /api/v1/vehicles`](#get-apiv1vehicles) | live vehicle positions | `no-store`, `refreshAfter` |
| [`GET /api/v1/alerts`](#get-apiv1alerts) | service alerts active now | `no-store`, `refreshAfter` |

### `GET /api/v1/stations`

Every station in the system, with enough to draw the map, search, and ask for
arrivals with no further request. There are 1,789 stations today, so fetch the list
once and revalidate it with its ETag (see [caching](#6-caching-versions-and-polling)).

```json
{
  "version": "38d2f22ad7a296e47e86dc80679a0a048cf3a81e",
  "formerIds": { "mongomery": "montgomery", "castroPlaza": "marketCastro" },
  "stations": [
    {
      "id": "embarcadero",
      "name": "Embarcadero",
      "lat": 37.792922,
      "lon": -122.396791,
      "lines": ["SF:J", "SF:K", "SF:L", "SF:M", "SF:N"],
      "modes": ["metro"],
      "operators": ["SF", "BA"],
      "platforms": [
        { "id": "SF:16992", "heading": "eastbound", "lines": ["SF:J", "SF:K", "SF:L", "SF:M", "SF:N"],
          "stops": ["SF:16992"], "formerIds": [] },
        { "id": "SF:17217", "heading": "westbound", "lines": ["SF:J", "SF:K", "SF:L", "SF:M", "SF:N"],
          "stops": ["SF:17217"], "formerIds": [] }
      ]
    }
  ]
}
```

| field | type | meaning |
|---|---|---|
| `version` | string | The data version (a commit of the `sf-transit` repo). It changes whenever the data does. The same value appears on the lines responses. |
| `formerIds` | `{string: string}` | Former station id → current station id. Apply it to saved station ids on every fetch. |
| `stations[].id` | string | Permanent station id. |
| `stations[].name` | string | Display name. |
| `stations[].lat`, `lon` | number | The station's position: the centroid of its platforms. Never null. |
| `stations[].lines` | [line id] | Every line serving any of its platforms, in the same order as `/lines`. |
| `stations[].modes` | [mode] | The modes of those lines, rail first. |
| `stations[].operators` | [operator] | Every operator serving the station. Operators with platforms here come first, then operators with none in the data yet (BART at Embarcadero, today). |
| `stations[].platforms[]` | Platform | In display order. |
| `…platforms[].id` | stop id | The platform's id: its primary stop. See [section 3](#3-identifiers). |
| `…platforms[].heading` | `northbound` \| `southbound` \| `eastbound` \| `westbound` | The direction a rider here travels. Treat it as open-ended. It is curated per platform, and follows how riders name the direction rather than a strict compass bearing. |
| `…platforms[].lines` | [line id] | Every line stopping at any of its stops. |
| `…platforms[].stops` | [stop id] | Its 511 stops, primary first. Usually just one. |
| `…platforms[].formerIds` | [stop id] | Ids it has had and lost. Usually empty. |

Only stations with at least one stop that 511 currently lists are included. A
station whose stops 511 has all dropped disappears until it is fixed or deleted.

### `GET /api/v1/stations/{id}`

One station in full. `{id}` may be a former id, which gets a `308` to the current
id. An unknown id is a `404`.

```json
{
  "version": "38d2f22ad7a296e47e86dc80679a0a048cf3a81e",
  "station": {
    "id": "montgomery", "name": "Montgomery", "lat": 37.789005, "lon": -122.401739,
    "lines": ["SF:J", "SF:K", "SF:L", "SF:M", "SF:N"], "modes": ["metro"], "operators": ["SF", "BA"],
    "platforms": [
      { "id": "SF:15731", "heading": "eastbound", "lines": ["SF:J", "SF:K", "SF:L", "SF:M", "SF:N"],
        "stops": ["SF:15731"], "formerIds": [],
        "name": null, "stopName": "Metro Montgomery Station/Downtown",
        "lat": 37.789219, "lon": -122.401351 }
    ],
    "transfers": [ { "to": "market2", "name": "Market & 2nd/Montgomery", "mode": "street" } ],
    "subways": [ { "id": "marketStreetSubway", "name": "Market Subway" } ],
    "alerts": [ { "id": "SF_15898", "header": "Folsom Street Fair this Sunday…", "…": "…" } ]
  }
}
```

It has every field of a station summary. Each platform additionally has:

| field | type | meaning |
|---|---|---|
| `name` | string \| null | Signage ("Platform 1", "To Castro"), where curated. Usually null. Not the 511 name. |
| `stopName` | string | 511's name for the primary stop. |
| `lat`, `lon` | number | The platform's position: the centroid of its stops, which are metres apart where there are several. |

And the station itself has:

| field | type | meaning |
|---|---|---|
| `transfers` | [{`to`, `name`, `mode`}] | Stations reachable on foot. `mode` is `indoor` or `street`. Every transfer also appears from the other station's side. Sorted by the other station's name. |
| `subways` | [{`id`, `name`}] | The subways this station is part of. |
| `alerts` | [Alert] | Alerts active now that touch this station, including agency-wide ones. See [alerts](#get-apiv1alerts). |

Sent with `Cache-Control: no-store` and no ETag, because `alerts` changes
independently of `version`.

### `GET /api/v1/lines`

Every line. There are 68 today, including owl, express and substitute services.

```json
{
  "version": "38d2f22ad7a296e47e86dc80679a0a048cf3a81e",
  "lines": [
    { "id": "SF:J", "shortName": "J", "name": "J Church", "color": "#FAA633", "textColor": "#FFFFFF",
      "mode": "metro", "hidden": false, "replaces": [] },
    { "id": "SF:LOWL", "shortName": "LOWL", "name": "LOWL Owl Taraval", "color": "#666666", "textColor": "#FFFFFF",
      "mode": "bus", "hidden": false, "replaces": ["SF:L"] }
  ]
}
```

| field | type | meaning |
|---|---|---|
| `id` | line id | |
| `shortName` | string | What goes in a line badge: "J", "38R", "NOWL". It can be four characters. |
| `name` | string | The full name: "J Church". |
| `color` | `#RRGGBB` \| null | The line's colour. |
| `textColor` | `#RRGGBB` \| null | 511's text colour for the badge. It is white almost everywhere, including on light colours where it barely reads (2.0:1 on the J's orange). **Pick black or white by WCAG contrast against `color` instead.** See [section 9](#9-a-suggested-shape-for-the-swift-sdk). |
| `mode` | `metro` \| `streetcar` \| `cableway` \| `bus` | Open-ended: BART and Caltrain will add modes. Draw rail modes as circle badges and buses as pills sized to their label. |
| `hidden` | bool | Curated as not for display. Hidden lines are still sent; leave them out of pickers. |
| `replaces` | [line id] | Lines this one substitutes for: the LOWL replaces the L overnight, and the KBUS replaces the K during an outage. A metro-only view should include lines that replace a line it shows. |

The order is the one to display: metro, then streetcar, then cableway, then bus.
Within each mode, numbered lines come first, in numeric order (5, 5R, 38, 38R),
then lettered ones.

### `GET /api/v1/lines/{id}`

One line, with its directions. An unknown id is a `404`.

```json
{
  "version": "38d2f22ad7a296e47e86dc80679a0a048cf3a81e",
  "line": {
    "id": "SF:N", "shortName": "N", "name": "N Judah", "color": "#00529C", "textColor": "#FFFFFF",
    "mode": "metro", "hidden": false, "replaces": [],
    "directions": [
      { "direction": 0, "headsign": "Ocean Beach",
        "stations": ["fourthKing", "secondKing", "embarcaderoBrannan", "…"],
        "stops": ["SF:15240", "SF:15237", "SF:17145", "…"],
        "shape": "SF:9717" },
      { "direction": 1, "headsign": "Caltrain/Ballpark",
        "stations": ["oceanBeach", "judah46", "…"], "stops": ["SF:15223", "SF:15216", "…"],
        "shape": "SF:9766" }
    ]
  }
}
```

| field | type | meaning |
|---|---|---|
| `directions[].direction` | `0` \| `1` | 511's direction id. It only has meaning within one line; the headsign is what to show. |
| `directions[].headsign` | string | Where this direction goes. |
| `directions[].stations` | [station id] | The stations served, in order: the line diagram. The first and last entries are the terminals. |
| `directions[].stops` | [stop id] | The same trip, stop by stop. Stops no station claims are left out, so this list can be longer than `stations`, where two stops fall in one station. |
| `directions[].shape` | shape id \| null | The path the line draws, a key into `/shapes`. When null, draw straight segments through the stops. |

A direction is the line's **most-run** stop sequence that way. Short turns and
branches are not represented yet; if they arrive it will be as a new field. **A
line may have one direction, not two** (the 30X runs one way today), so never
assume two.

### `GET /api/v1/shapes`

The path every direction draws, for putting lines on a map. It is about 426 KB
raw and 88 KB compressed, and changes only when 511 redraws a line, so revalidate
it with its ETag.

```json
{ "shapes": { "SF:102": [[-122.396968, 37.795436], [-122.396784, 37.795471], "…"] } }
```

Each point is **`[lon, lat]`**, GeoJSON order and the reverse of every other part
of this API. It is simplified to within half a metre of 511's path. Its ETag is its
own content hash, not the data `version`, so curation edits do not invalidate it.

### `GET /api/v1/arrivals`

The next arrivals at one or more platforms.

`GET /api/v1/arrivals?platforms=SF:16992,SF:17217&limit=6`

| parameter | required | meaning |
|---|---|---|
| `platforms` | yes | 1–50 comma-separated platform ids. Any stop or former id of a platform also works. |
| `limit` | no | Arrivals per platform. Default 6, maximum 30. |

```json
{
  "fetchedAt": 1790197438,
  "feedAt": 1790113897,
  "refreshAfter": 184,
  "platforms": {
    "SF:16992": [
      { "line": "SF:L", "direction": 1, "headsign": "Embarcadero Station", "time": 1790113947,
        "kind": "arrival", "terminates": true, "trip": "SF:12134484_M11", "vehicle": "SF:2019" }
    ],
    "SF:17217": [
      { "line": "SF:K", "direction": 0, "headsign": "Balboa Park", "time": 1790114045,
        "kind": "departure", "terminates": false, "trip": "SF:12133095_M11", "vehicle": "SF:2070" }
    ]
  }
}
```

**`platforms` is keyed by platform id.** An id sent that is another stop or a
former id of a platform is answered under that platform's current `id`. Two ids for
the same platform produce one entry. A stop that belongs to no platform is answered
under itself. Every platform asked for is present in the answer, as an empty list
when nothing is due.

| field | type | meaning |
|---|---|---|
| `fetchedAt` | int \| null | When the server last downloaded the feed. Null before its first fetch. |
| `feedAt` | int \| null | 511's own time for that feed: **how old the predictions are**. |
| `refreshAfter` | int | Seconds until the server can have newer data. Poll no sooner. See [polling](#polling). |
| `…[].line` | line id | |
| `…[].direction` | `0` \| `1` | |
| `…[].headsign` | string | Where the vehicle is going. |
| `…[].time` | int | The predicted time, epoch seconds. Only arrivals at or after the server's current time are included, soonest first. |
| `…[].kind` | `arrival` \| `departure` | `departure` means the trip **starts at this platform**, and `time` is when it leaves. |
| `…[].terminates` | bool | The trip **ends at this platform**: nobody boards it here. |
| `…[].trip` | string | 511's trip id. Useful to tie the same trip across platforms within one answer; never store it. |
| `…[].vehicle` | string \| null | The vehicle serving the trip, when known. It matches `Vehicle.id` in `/vehicles`. |

### `GET /api/v1/vehicles`

Live positions of vehicles in service.

`GET /api/v1/vehicles?line=SF:L,SF:N`

| parameter | required | meaning |
|---|---|---|
| `line` | no | Comma-separated line ids, up to 100. With none, every vehicle in service. |

```json
{
  "fetchedAt": 1790197438,
  "feedAt": 1790113896,
  "refreshAfter": 184,
  "vehicles": [
    { "id": "SF:2001", "line": "SF:N", "direction": 0, "trip": "SF:12135883_M11",
      "lat": 37.761879, "lon": -122.473503, "bearing": 255.0, "speed": 4.44,
      "stop": "SF:15198", "status": "stoppedAt" }
  ]
}
```

| field | type | meaning |
|---|---|---|
| `id` | string | The vehicle. |
| `line`, `direction`, `trip` | | What it is running. `direction` may be null. |
| `lat`, `lon` | number | Where it is. |
| `bearing` | number \| null | Degrees clockwise from north. **Null for about 1 vehicle in 5**, because 511 sends 0 when it does not know, so a true 0 is indistinguishable and treated as unknown. Draw a dot, not an arrow, when null. |
| `speed` | number \| null | Metres per second. Null in the same way, and more often. |
| `stop` | stop id \| null | The stop it is at or heading to. |
| `status` | `incomingAt` \| `stoppedAt` \| `inTransitTo` \| null | How it relates to `stop`. Treat as open-ended. |

There is **no per-vehicle report time**, because 511 stamps a whole feed with one
time. `feedAt` is the age of every position in the answer. Positions move only when
the server fetches, so animate a marker to its new position rather than jumping it.

### `GET /api/v1/alerts`

Service alerts active now.

`GET /api/v1/alerts?line=SF:38&station=powellOfarrell&platforms=SF:15817`

| parameter | required | meaning |
|---|---|---|
| `line` | no | Comma-separated line ids. |
| `station` | no | Comma-separated station ids. |
| `platforms` | no | Comma-separated platform ids. Any stop of a platform also works. |

Each takes up to 50 ids. With no parameters, every active alert is returned. With
any, an alert is returned if it touches any line, station or platform named, **plus
every agency-wide alert**, since those touch everything.

```json
{
  "fetchedAt": 1790197438,
  "feedAt": 1790113697,
  "refreshAfter": 1204,
  "alerts": [
    { "id": "SF_15752",
      "header": "38, 38R STOP TEMP. MOVED Board at O'Farrell/Mason",
      "description": "38, 38R STOP TEMP. MOVED Board at O'Farrell/Mason",
      "activePeriods": [ { "start": 1789369200, "end": 1791356399 } ],
      "lines": ["SF:38", "SF:38R"], "stops": ["SF:15817"], "stations": ["powellOfarrell"],
      "url": null }
  ]
}
```

| field | type | meaning |
|---|---|---|
| `id` | string | 511's alert id. |
| `header` | string | A short summary. 511 often repeats it word for word as `description`. |
| `description` | string | English only; 511 sends no translations. |
| `activePeriods` | [{`start`, `end`}] | When it applies. Either bound may be null, meaning open-ended. |
| `lines`, `stops` | lists | What 511 says it affects. **Both empty means agency-wide.** |
| `stations` | [station id] | The stations those stops belong to. |
| `url` | string \| null | |

511 does not categorise alerts: "stop moved" and "stop closed" differ only in the
text.

### Errors

| status | `error` | when |
|---|---|---|
| 400 | `bad-request` | A parameter is missing or malformed: an id not shaped like one, too many ids, a bad `limit`. `message` says which. |
| 404 | `not-found` | An unknown station or line id. |
| 503 | `unavailable` | The server has no data loaded yet, or realtime is off. `message` says why. Retry later. |

A `308` on a station id is not an error: it is a former id, redirected. Treat
`error` as open-ended: an unknown code should be handled as the status's general
case.

Only the errors above carry a `Problem`. A path that does not exist (`404`) or a
wrong method (`405`) gets the framework's own `{"detail": "…"}`, so decode error
bodies leniently and fall back to the status code.

---

## 5. Realtime semantics

**Reading a station board.** For each arrival:

| `kind` | `terminates` | show it as |
|---|---|---|
| `arrival` | `false` | an ordinary arrival: "N to Ocean Beach in 4 min" |
| `departure` | `false` | the trip starts here: "departs 12:04". At a terminal, this is the time that matters. |
| `arrival` | `true` | the trip ends here. Nobody boards it: hide the row, or label it. |

Decide "ends here" from `terminates`, never from `headsign`. The headsign is taken
from the line and direction, so a short-turning N reads "Caltrain/Ballpark" when that
particular trip stops at Embarcadero. `terminates` is per trip and is always right.
Per-trip headsigns may arrive later in the same field.

**Counting down.** `time` is absolute. Count down against the device clock, and
the countdown stays correct between polls. What goes stale is the prediction
itself. Show its age (`now − feedAt`) when it grows large, say over five minutes.

**Vehicles and arrivals together.** `Arrival.vehicle` matches `Vehicle.id`, so a
board can show where an approaching vehicle is right now.

---

## 6. Caching, versions and polling

### Versions

`/api/v1/` is a contract. **Within v1, changes are additive only**: new fields,
new endpoints, new values in open-ended enums. A change that would break a client
becomes `/api/v2/`, served alongside v1. A client must therefore ignore fields it
does not know.

### Reference data: stations, lines, line detail, shapes

These send an `ETag` and `Cache-Control: no-cache`. Keep the body, and send the
tag back as `If-None-Match` when revalidating. A `304` means the copy is current.
`URLSession`'s default `URLCache` does all of this on its own.

The ETag covers both the data version and the shape of the response, so after a
deploy that changes a response, a revalidation gets the new body, never a `304` for
an old-shaped one. Cloudflare delivers the tags weak (`W/"…"`); send them back as
received.

Revalidate the station list on launch and when returning to the foreground. It
changes when the data is edited: several times a day while stations are being
curated, rarely after that.

### Station detail

`Cache-Control: no-store`, with no ETag: its alerts are live. Fetch it when a
station is opened.

### Polling

Arrivals, vehicles and alerts are `no-store` and carry **`refreshAfter`**: the
seconds until the server's next fetch from 511, plus a margin. Schedule the next
request that many seconds later, and **never on a fixed timer**. The server's own
fetch interval varies: 3 minutes for arrivals and vehicles in development, possibly
20 seconds in production, 20 minutes for alerts. `refreshAfter` is never below 10.

Stop polling while the app is in the background, and poll once as it returns.

---

## 7. Rules every client must follow

1. **Enums are open.** `mode`, `heading`, `kind`, `status`, a transfer's `mode` and
   the error codes will all gain values. Decode an unknown value into an `unknown`
   case, never throw. One failed field must not fail a whole response.
2. **Ignore unknown fields.** v1 grows by adding them.
3. **Ids are opaque.** Pass back what was given. Never parse the `SF:` prefix.
4. **Store only station ids, platform ids, line ids and operator codes.** Never
   store trip, vehicle or shape ids.
5. **Resolve stored platform ids** against the latest station list: by `id`, then
   `stops`, then `formerIds` ([section 3](#3-identifiers)). A home or favourite
   with no match is gone. Say so; do not drop it silently.
6. **Apply `formerIds` to stored station ids** on every station-list refresh, and
   save the result.
7. **Read arrivals by platform id.** Resolve ids first, then read the answer under
   the resolved platform's `id`.
8. **Poll on `refreshAfter`,** not on a constant.
9. **Don't assume counts.** A line may have one direction, a station many
   platforms, a platform many stops. Anything can have no lines.
10. **Mind the two coordinate orders:** `lat`/`lon` fields everywhere except
    `/shapes`, which is `[lon, lat]`.
11. **Choose badge text colour by contrast,** not from `textColor`.

---

## 8. Migrating the App Store app's saved data

The shipping app predates this API. It keeps bare stop codes (`16992`) and the old
station ids.

- **Stop codes → platform ids:** prefix `SF:` (`16992` → `SF:16992`), then resolve
  as in [section 3](#3-identifiers). This is the one place a client may build an
  id, and only for this migration.
- **Station ids:** map through `formerIds`. `mongomery` → `montgomery` is the
  best-known one; others come from merges (`castroPlaza` → `marketCastro`).
- Anything that fails to resolve is a platform or station that no longer exists.
  Tell the person, once.

Run the migration once, after the first successful `/stations` fetch, and store the
results in the new form.

---

## 9. A suggested shape for the Swift SDK

These are suggestions, not requirements; the rules in section 7 are the
requirements.

- **Client.** An `actor MuniClient` with `async throws` methods, one per endpoint,
  on a `URLSession` whose `URLCache` handles the ETags. The base URL comes from
  configuration.
- **Models.** `Codable` structs mirroring the responses, with camelCase keys (they
  already match Swift's).
- **Open enums.** `Mode`, `Heading`, `ArrivalKind`, `VehicleStatus`, `TransferMode`
  and `ErrorCode` as enums with an `unknown(String)` case and a hand-written
  `init(from:)`, for example:
  ```swift
  enum Mode: Hashable, Codable {
      case metro, streetcar, cableway, bus, unknown(String)
      init(from decoder: Decoder) throws {
          let raw = try decoder.singleValueContainer().decode(String.self)
          switch raw {
          case "metro": self = .metro
          case "streetcar": self = .streetcar
          case "cableway": self = .cableway
          case "bus": self = .bus
          default: self = .unknown(raw)
          }
      }
  }
  ```
- **The network.** A `Network` value built from `/stations` and `/lines`, with
  indexes: station by id, line by id, and a platform index covering every
  platform's `id`, `stops` and `formerIds`. That index is how stored platform ids
  resolve. Rebuild it whenever the station list changes.
- **Polling.** An `ArrivalsFeed` or `AsyncSequence` per set of platforms that
  fetches, publishes, and sleeps for `refreshAfter`. The same for vehicles.
- **Coordinates.** Convert to `CLLocationCoordinate2D` in one place, which is where
  the `[lon, lat]` order of `/shapes` gets handled.
- **Badge colours.** Choose black or white by WCAG contrast: relative luminance
  `L`, then white when `1.05 / (L + 0.05) ≥ (L + 0.05) / 0.05`, otherwise black.
  That gives black on the J's orange (10.6:1, where white is 2.0:1) and white on
  the N's blue (7.8:1).
- **Tests.** Decode the examples in this document, plus a response with an unknown
  enum value and an unknown field, both of which must decode.
