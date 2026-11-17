# Canopy Transit Editor

> **Looking for the Muni+ app?** This repo is still its App Store support page.
> [Open an issue](../../issues) to report a bug or a data problem.

The data side of Muni+: an editor for the station and line data the app is built
on, and the files it publishes.

**Status:** being reorganised. The old hosted editors have been retired, and the
new editor doesn't have a home yet. Data is still published by hand in the
meantime.

## Branches

| Branch | What it holds |
|---|---|
| `master` | Code: the API and editor, tools, and the app's hand-curated `appdata/data.json` |
| `static-data` | The editor's own data: curated stations, lines and shapes in `curation/`, plus a 511 GTFS snapshot in `snapshot/`. Formerly the `narenh/sf-transit` repo, history included |
| `static` | **Transit Static**: what transit-static.canopysf.com serves (`public/data.json`, `public/map.pdf`, `public/map-dark.pdf`); muni.canopysf.com is an alias. Deployed with `wrangler deploy` from this branch, using the wrangler installed on `master` (`package.json`) |
| `claude/beautiful-cerf-23u8yd` | A standalone, map-first version of the editor for `appdata/data.json` (`editor/`, Node). Read-only as it was last deployed |

## Where the data lives

- **What the app reads today:** `data.json`, edited by hand on `master` and
  copied to `static` to publish.
- **The future source of truth:** the editor's data on `static-data`. It is not
  in the app's format yet, and an exporter to `data.json` still needs writing.
  Until then the two are kept in step by hand, and they have drifted apart:
  - some station ids differ (`mongomery` vs `montgomery`);
  - Balboa Park is grouped differently;
  - the fixes from 14 November 2026 are only in `data.json`.

## Layout of `master`

| Path | What it is |
|---|---|
| `appdata/` | `data.json` (Metro and the F, hand-curated), `bus.json` (generated), `map.pdf` |
| `tools/` | `build_bus_data.py` (bus and cable car stations) and `map_dark.py` (the dark-mode system map). See [tools/README.md](tools/README.md) |
| `api/` | The Python v1 API and its built-in editor, which saved to `static-data`'s data. Retired; kept for its editor code |
| `swift/CanopyTransitKit` | Swift client for the v1 API. Shelved |

## Related repos

- **canopy-transit**: Muni+, the iOS app.
- **canopy-transit-relay**: the Worker behind transit-relay.canopysf.com
  (alias muni-relay.canopysf.com) that translates prediction APIs.

## License

© Naren Hazareesingh
