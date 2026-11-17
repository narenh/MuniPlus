# Canopy Transit Editor

The editor for the station and line data on branch `static-data`, and a public,
read-only map of the same data. A FastAPI server (`transit_editor/`) and a
vanilla-JS frontend with no build step (`frontend/`).

Served at https://transit-editor.canopysf.com:

- `/` — the editor, behind a password login (`/login`, `/logout`); its API is
  `/api/*`, its session cookie has path `/`
- `/map/` — the same page, read-only, no login; its API is `/map/api/*`
- `/health` — whether the data checkout is loaded
- `/editor`, `/editor/*` — the editor's old home; 308 to the same path at `/`

The editor's assets (`/css`, `/js`, `/img`, `/v/<version>/`) share the root with
these routes; the server refuses to start if a frontend folder would shadow one.

The design, data format and rules are in [PLAN.md](PLAN.md).

## What it does

- Clones `DATA_REPO` at `DATA_BRANCH` into `DATA_DIR/data` on startup and pulls it.
- **Save** writes the changed `curation/` files, rebases on the remote, commits
  (as `GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL`) and pushes with `DATA_DEPLOY_KEY`.
  A rebase conflict is a 409, never resolved automatically.
- **Review** lists 511 stops no station claims, with a proposed station, plus
  dead platforms and stations.
- **Snapshot refresh** downloads 511's GTFS zip and line list from the Canopy
  Transit Relay (`RELAY_URL/static/datafeeds` and `/static/lines`), shows how
  they differ from the committed snapshot, and commits `snapshot/<operator>/`
  only when asked. The relay holds the 511 key and keeps each file for a day;
  the editor has no 511 key of its own.

The editor never calls 511 directly and serves nothing to the Muni+ app.

## Layout

| Path | What it is |
|---|---|
| `transit_editor/main.py` | App assembly and startup (clone, load) |
| `transit_editor/settings.py` | Every setting, from the environment or `editor/.env` |
| `transit_editor/editor/` | Login, editor API, save, review, snapshot refresh, page serving |
| `transit_editor/endpoints/` | `/map/api/state`, `api/shapes`, `/health`, HTTP caching helpers |
| `transit_editor/relay.py` | The client for the relay's static files |
| `transit_editor/data/` | The checkout (read side), the derived network, validation, shapes |
| `transit_editor/models/` | The data files' models and the wire shapes |
| `transit_editor/ingest/` | GTFS zip → snapshot, and the station proposer |
| `frontend/` | The page served at `/` and `/map/` |
| `tests/` | pytest; no network (the relay is an `httpx.MockTransport`, git remotes are local bare repos) |

## Settings

See [.env.example](.env.example). All optional; an empty value means the default.

| Variable | Default | |
|---|---|---|
| `DATA_DIR` | `/data` | Holds the checkout (`data/`) and a pending refresh (`snapshot-pending/`). Persistent |
| `DATA_REPO` | `git@github.com:narenh/canopy-transit-editor.git` | |
| `DATA_BRANCH` | `static-data` | |
| `DATA_DEPLOY_KEY` | | Private key with write access. Without it an SSH `DATA_REPO` is read-only |
| `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL` | | Without both the editor is read-only |
| `EDITOR_PASSWORD` | | Without it `/` is served read-only to anyone and nothing can be saved |
| `SESSION_SECRET` | | Signs the session cookie |
| `RELAY_URL` | `https://transit-relay.canopysf.com` | |
| `OPERATORS` | `["SF"]` | JSON list |

## Running locally

```sh
cd editor
python3.13 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

To run it against a local copy of the data, without touching GitHub:

```sh
git clone --bare --branch static-data <this repo> /tmp/data.git
DATA_DIR=/tmp/editor-data DATA_REPO=/tmp/data.git \
GIT_AUTHOR_NAME=me GIT_AUTHOR_EMAIL=me@example.com \
EDITOR_PASSWORD=<anything> SESSION_SECRET=<anything> \
  .venv/bin/uvicorn transit_editor.main:app --port 8000
```

A local path as `DATA_REPO` needs no deploy key; saves push to that bare repo.

## Deploying (Coolify)

- Build pack: Dockerfile. Base directory `/editor`, Dockerfile `/Dockerfile`
  (that is, `editor/Dockerfile` with `editor/` as the build context).
- Port 8000. Persistent volume at `/data`.
- Watch paths: `editor/**`.
- One container, one worker (the Dockerfile's `CMD` already says so).
