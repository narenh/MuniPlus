# Muni+ Atlas

A map-first editor for `appdata/data.json`. It draws the eight Muni lines on a
dark basemap, lets you drag individual platform poles onto the right corner,
reorder a line's stops by dragging them in a route diagram, and commits the
result straight to git as a minimal diff.

It only ever touches `appdata/data.json`. `bus.json`, `tools/` and the generator
are out of its reach by design — `data.json` is the hand-curated file, and this
is the hand.

## Running it locally

```sh
node editor/server.js          # http://localhost:8787
```

No dependencies, no build step, no `npm install`. Node 20+.

The repo it edits defaults to the parent of `editor/`, so running it from a
checkout just works.

## Deploying on Coolify

**There is no authentication in this app.** Anything that can reach the port can
commit to the repo. Put Coolify's proxy auth (or an access tier, or a private
network) in front of it. That is what the subdirectory is for — the editor is a
separate service from the Worker that serves `appdata/`.

**The build context is the repository root, not `editor/`:**

```sh
docker build -f editor/Dockerfile -t atlas .
docker run -p 8787:8787 atlas          # works, read-only, no env vars
```

In Coolify: Base Directory `/`, Dockerfile Location `/editor/Dockerfile`.

### Read-only is a first-class mode

With no environment variables at all the image serves a fully working, read-only
Atlas: the whole map, the route strips, the inspector, the feed check — you just
cannot save. That is deliberate, so a deploy succeeds before any secret exists.

Atlas degrades instead of failing. It writes only when it has somewhere to write,
and says which of these is stopping it:

| situation | what happens |
|---|---|
| a git checkout at `REPO_ROOT` | editable; Save commits |
| a checkout, but the file is not writable | read-only |
| no `.git` at `REPO_ROOT` | read-only, serving the file that is there |
| `REPO_ROOT` empty or missing | read-only, serving the copy baked into the image |
| `READ_ONLY=1` | read-only, whatever else is true |
| no `data.json` anywhere | the UI loads and explains where it looked |

In read-only mode the editing affordances are hidden rather than left to fail,
the branch chip turns amber and reads *Read-only*, and hovering it says why.

To make it editable, give it a checkout, either by mounting one:

| variable | meaning |
|---|---|
| `REPO_ROOT` | path to the git checkout (default `/srv/repo`) |
| `DATA_FILE` | file to edit, relative to the repo root (default `appdata/data.json`) |

…or by letting the entrypoint clone one:

| variable | meaning |
|---|---|
| `GIT_REPO_URL` | e.g. `https://x-access-token:TOKEN@github.com/narenh/MuniPlus.git` |
| `GIT_BRANCH` | branch to check out and push to |
| `GIT_PUSH` | `1` to push after every commit; otherwise commits stay local |
| `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL` | commit identity |
| `READ_ONLY` | `1` to serve the UI but refuse every write |
| `PORT` | default `8787` |

None of these are required. Every one has a working default.

A token in `GIT_REPO_URL` is written into `.git/config` inside the container, so
use a token scoped to this one repo and rotate it like any other deployed secret.

If `GIT_PUSH` is off, commits pile up in the container's checkout and are lost
when the container is replaced. Either turn pushing on, or mount `REPO_ROOT` on a
persistent volume.

## The data model

A station is one of two shapes, and **the shape is its own discriminator** —
there is no `kind` field. A station either has `levels` (and `exits`), or a flat
`platforms` list. A stored kind could contradict the shape, and "underground"
was already wrong for a station whose levels go up.

**Levelled** — platforms live on `levels`, keyed by depth. Depth is a signed
ordinal, not a measurement: **0 is street, negative is below it, positive is
above**. Elevated levels are real — Balboa Park's BART tracks are on a viaduct —
so a positive depth is not a mistake. A level carries its name, an optional `agency`, and
`isIsland`, which is hand-authored and legitimately `null`. A mezzanine is just
a level with no platforms. `exits` are the doors: a name, the level each lands
on, `stairs` / `escalator` / `elevator` / `closed` booleans, and coordinates.

**Street** — a flat `platforms` list. No levels, no exits, no island flag;
every platform already has precise coordinates, so grouping by heading axis is
derivable rather than stored.

Every platform carries `id` (the 5-digit stop code), `heading`, `lines`, an
optional `name` for signage ("Platform 1", "To Castro"), and — where some but
not all of its lines end there — `terminates`. That last one is per *(platform,
line)* rather than per platform because Embarcadero needs it: J/K/L/M/S
terminate while the N runs through, on the same concrete.

Transfers are objects with a `mode`. An `indoor` link is validated as
reciprocal, because you cannot build a passage you can only walk one way.

### What is derived, and never editable

| field | from |
|---|---|
| `station.latitude` / `longitude` | **exits** underground, **platforms** on the surface |
| `station.lines` | the union of its platforms' lines |

Underground platform coordinates deliberately do **not** feed the station
coordinate — they are below ground, and what a rider walks to is a door. They
are kept because they let you rank exits by distance to a platform.

An underground station with no exits yet therefore has `latitude: null`. The
editor still shows it, anchored to its platforms so you can find it and author
its doors; the stored value stays null until a door exists.

## Using it

| | |
|---|---|
| **Line rail** (left edge) | click a line to focus it; click again for all lines |
| **Route strip** | the focused line's stops in order — drag the grip to reorder, `−` to remove |
| **Map** | click a station or pole to select; **drag a pole** to move it |
| **Inspector** (right) | the selected station: levels and exits underground, platforms on the surface |
| **Exits** | added from the inspector, dropped on the station, then dragged onto the real door |

Keys: `⌘K` find anything · `⌘S` save · `⌘Z` / `⇧⌘Z` undo, redo · `↑` `↓` walk the
focused line · `F` fit line · `A` all lines · `P` `L` `T` toggle poles, labels,
transfers · `N` reset bearing · `Esc` deselect.

### Levels and exits

Levels are added, renamed, re-depthed, reordered and deleted from the inspector,
and are always listed top of the stack first.

Because a level's depth *is* its identity, moving one up or down swaps the two
depths — and every exit that lands on either level follows the level it belongs
to, not the number it used to carry. Typing a new depth directly does the same
repointing. Deleting a level tells you how many platforms go with it and how many
exits will need a new home.

An exit starts with no coordinate. **Place this exit on the map** drops it on the
station and flies there; from then on you drag the door onto its real position,
and the station's coordinate follows. Closed exits render struck through in red,
on the map and in the inspector.

`isIsland` is a three-state control — Unset / Island / Separated — and the editor
proposes nothing. Geometry cannot answer it: Stonestown and Holloway are the same
construction with opposite geometric signatures, so it is authored by hand.

### Stations and their platforms

A station and its poles are drawn as separate things. From about z15 each pole
is tied to its station by a dashed leader, and from z15.6 it carries its stop
code, so you can tell two poles apart and see which station each belongs to.

Poles are drawn **above** station dots, which matters underground: Powell's two
platforms sit 38 m from the station centre, so under the station dot they were
neither visible nor clickable. Now you can grab one and drag it out to the real
street entrance, and the leader keeps showing what it belongs to.

### Saving

**Write only** writes the file and leaves it uncommitted, so you can inspect it
with `git diff`. **Commit** writes and commits, and pushes when `GIT_PUSH=1`.

The file is written with the same 2-space indent and trailing newline the repo
already uses, and a JSON round-trip is byte-identical, so a one-pole move is a
two-line diff and nothing else moves.

Unsaved edits are mirrored into `localStorage` and offered back after a reload.
The draft is dropped if the file has been committed from elsewhere in the
meantime, so it can never silently revert someone else's work.

### Transfers

`transferStations` is one-way: a link from A to B says nothing about B to A, and
that asymmetry is deliberate, so Atlas never symmetrises anything on its own.

On the map a transfer is a dotted walking path with a chevron at each end it
actually points to — one chevron for a one-way link, two for a mutual one. The
paths only appear once you are zoomed in far enough for the walk to mean
anything (about z13.6), and they are straight: data.json records that a walk
exists, never its route, so drawing a path through the streets would be a claim
the file does not make.

The inspector groups a station's links into **both ways**, **one way out** and
**one way in**. That last group is links pointing *at* this station, which are
invisible in the raw file and are exactly what you need when deciding whether a
link should be mutual — each one has a **+** to add the reciprocal, always as an
explicit click. Hovering a link lights its path on the map.

### The feed check

"Check feed" pulls the live SFMTA GTFS feed and compares every platform against
it. A platform more than 25 m from its feed coordinate is flagged, and its card
offers **snap to feed**. A stop code the feed no longer carries is flagged too.

The feed is a reference, not an authority — `data.json` deliberately disagrees
with it in places (see `tools/README.md`), so nothing is ever snapped for you.

### What it refuses to save

The server re-validates before writing and rejects a document outright when a
stop code appears under two stations, a station id is duplicated, a station has
no platforms, a stop code is not five digits, or a line, subway or transfer
points at a station that does not exist. The first two are the hard invariants
`tools/build_bus_data.py` fails its build on.

Softer things — a station on a line that does not list that line back — surface
as warnings in the top bar and the save sheet, and do not block a commit, because
that judgement is yours.

## How it is put together

```
server.js          HTTP + JSON API. No dependencies.
entrypoint.sh      clones REPO_ROOT if asked; never fails the container
lib/data.js        read / validate / atomic write
lib/git.js         git CLI wrapper, scoped to the one file
lib/gtfs.js        zip reader + CSV parser (~140 lines, no deps)
public/js/store.js state, undo/redo, the human-readable change list
public/js/map.js   MapLibre layers, pole dragging, camera
public/js/strip.js the route diagram and its drag-to-reorder
public/js/inspector.js  station and platform forms
public/js/ui.js    palette, save sheet, history, toasts
```

MapLibre GL and the Carto Dark Matter basemap load from their CDNs, so the
browser needs internet — which it needs for the basemap regardless.
