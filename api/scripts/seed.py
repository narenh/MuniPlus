"""Seed a fresh sf-transit tree from the data the App Store build ships today.

    .venv/bin/python scripts/seed.py --out .local/sf-transit --date 2026-09-22

One-off. It reads, never writes:

* the hand-curated metro file, ``appdata/data.json`` on the editor branch
  (``--metro-ref``), which is newer than master's and restructured around levels;
* the generated surface file ``appdata/bus.json`` and ``tools/station-overrides.json``;
* the 511 GTFS zip and ``/transit/lines`` response already downloaded to
  ``--upstream``. Nothing here calls 511.

It writes the curation files and ``snapshot/SF`` through ``app.models.files`` (the
only writer), and a reconciliation report to ``--report``, kept outside the tree so
it never lands in sf-transit. The report exists so a person can confirm, without
re-deriving anything, that no platform, station, transfer or hand-entered value
was lost on the way.

The merge follows the load rule in tools/README.md: data.json first, and a
bus.json station with the same id contributes only the platforms not already
there, while data.json's name and transfers stand.
"""

import argparse
import hashlib
import json
import math
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

from app.ingest.gtfs_static import build_snapshot
from app.models import files
from app.models.curation import (
    Curation,
    IgnoredFile,
    IgnoredStop,
    LineOverride,
    LinesFile,
    Platform,
    Station,
    StationsFile,
    Subway,
    Transfer,
)
from app.models.ids import ref, upstream_of
from app.models.snapshot import Snapshot

API = Path(__file__).resolve().parent.parent
REPO = API.parent
OPERATOR = "SF"

RENAMES = {"mongomery": "montgomery"}
"""The one station rename. ``mongomery`` was a typo that shipped, so it lives on in
App Store users' favourites and must survive as a former id."""

TYPO_MERGES = {
    "bayshoreLEland": ("bayshoreLeland", "the feed spells its stop 'Bayshore Blvd &L eland Ave', a misplaced space"),
    "middlePointFairFax": ("middlePointFairfax", "the feed spells its stop 'Middle Point Rd & Fair Fax Ave'"),
}
"""Stations the old generator minted only because a feed typo made one corner look
like two, keyed by the typo id, with the station they belong to and the typo. Found
by the validator's check for station ids that differ only in case. The typo ids
shipped in bus.json, so they survive as former ids."""

NEEDS_A_PERSON = {
    "SF:17181": (
        "columbusChestnut",
        "SFMTA's summer feed (2026-07-23 to 08-28, what bus.json was built from) names it "
        "'Columbus Ave & Chestnut St'; 511's current feed names it '{name}' with the same "
        "coordinates. Rebuilding bus.json from 511 files it under columbusLombard instead. "
        "Left in columbusChestnut until someone checks the pole.",
    ),
}
"""Platforms kept where they are but flagged, with the evidence, for a person to check."""

AGENCIES = {"bart": "BA", "caltrain": "CT"}
"""data.json's agency names -> 511 operator codes."""

REPLACEMENTS = {"FBUS": "F", "KBUS": "K", "LOWL": "L", "NBUS": "N", "NOWL": "N", "TBUS": "T"}
"""Substitution and owl buses, each tracing its rail line stop for stop (tools/README.md)."""


# MARK: - Inputs


@dataclass
class Inputs:
    metro: dict
    metro_source: str
    bus: dict
    overrides: dict
    snapshot: Snapshot
    sources: dict[str, str]


def git_show(ref_name: str, path: str) -> tuple[str, bytes]:
    sha = subprocess.run(["git", "-C", str(REPO), "rev-parse", ref_name], check=True, capture_output=True, text=True).stdout.strip()
    blob = subprocess.run(["git", "-C", str(REPO), "show", f"{sha}:{path}"], check=True, capture_output=True).stdout
    return sha, blob


def fetched_at_of(headers: Path, fallback: Path) -> datetime:
    """When the zip came back from 511: the response's own ``Date`` header, saved
    next to it. The file's mtime is the fallback and is a second later (16:12:37
    PDT against ``Date: 23:12:36 GMT``), because it is when the write finished."""
    if headers.exists():
        for line in headers.read_text().splitlines():
            name, _, value = line.partition(":")
            if name.strip().lower() == "date":
                return parsedate_to_datetime(value.strip()).astimezone(UTC)
    return datetime.fromtimestamp(int(fallback.stat().st_mtime), UTC)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load(args) -> Inputs:
    metro_sha, metro_blob = git_show(args.metro_ref, "appdata/data.json")
    bus_blob = args.bus.read_bytes()
    overrides_blob = args.overrides.read_bytes()
    zip_path = args.upstream / "gtfs-511-SF.zip"
    lines_path = args.upstream / "lines-511-SF.json"
    fetched = fetched_at_of(args.upstream / "gtfs.headers", zip_path)
    snapshot = build_snapshot(zip_path, lines_path, OPERATOR, fetched)
    return Inputs(
        metro=json.loads(metro_blob),
        metro_source=f"{args.metro_ref} ({metro_sha}):appdata/data.json",
        bus=json.loads(bus_blob),
        overrides=json.loads(overrides_blob),
        snapshot=snapshot,
        sources={
            "metro": f"{args.metro_ref} @ {metro_sha} : appdata/data.json (sha256 {sha256(metro_blob)})",
            "bus": f"{args.bus} (sha256 {sha256(bus_blob)})",
            "overrides": f"{args.overrides} (sha256 {sha256(overrides_blob)})",
            "gtfs": f"{zip_path} (sha256 {snapshot.meta.sha256})",
            "lines": f"{lines_path} (sha256 {sha256(lines_path.read_bytes())})",
            "fetchedAt": fetched.isoformat(),
        },
    )


# MARK: - Report


@dataclass
class Report:
    sections: list[tuple[str, list[str]]] = field(default_factory=list)

    def section(self, title: str, lines: list[str] | None = None) -> list[str]:
        body = list(lines or [])
        self.sections.append((title, body))
        return body

    def render(self) -> str:
        out = []
        for title, body in self.sections:
            out += [f"## {title}", ""] + (body or ["(none)"]) + [""]
        return "\n".join(out)


def real(entries: dict) -> dict:
    """Keys starting with ``/`` are comments in station-overrides.json (``//``)."""
    return {k: v for k, v in entries.items() if not k.startswith("/")}


def rid(station_id: str) -> str:
    return RENAMES.get(station_id, station_id)


def pid(code: str) -> str:
    return ref(OPERATOR, code)


def metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    # Equirectangular is exact enough at the scale of one pole's error (metres).
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(x, math.radians(lat2 - lat1)) * 6_371_000


def metro_platforms(station: dict) -> list[dict]:
    """Flattened: levels in listed order, each level's platforms in order, then any
    flat list. (No station on the editor branch has both.)"""
    out = []
    for level in station.get("levels", []):
        out += level["platforms"]
    return out + station.get("platforms", [])


# MARK: - Conversion


@dataclass
class Seed:
    curation: Curation
    origin: dict[str, str]
    """station id -> ``metro``, ``bus`` or ``metro+bus``."""
    gained: list[str]
    """data.json stations that bus.json added platforms to."""
    dropped: dict[str, list[str]]
    """What data.json said that has no place in the new model, by kind."""
    typo_merges: dict[str, tuple[str, str]]
    """The ``TYPO_MERGES`` this seed applied."""


def convert(inputs: Inputs, verified_on: date, report: Report, typo_merges: dict = TYPO_MERGES) -> Seed:
    metro, bus, snapshot = inputs.metro, inputs.bus, inputs.snapshot
    overrides = inputs.overrides
    stations: dict[str, Station] = {}
    origin: dict[str, str] = {}
    owner: dict[str, str] = {}  # platform code -> station id
    heading_of: dict[str, str] = {}
    lost: list[str] = []  # platform codes that ended up nowhere, with the reason
    dropped: dict[str, list[str]] = defaultdict(list)
    agencies_unknown: list[str] = []

    # --- data.json: authoritative, and every station it has is hand-curated.
    for s in metro["stations"]:
        sid = rid(s["id"])
        platforms = []
        for level in s.get("levels", []):
            dropped["levels"].append(
                f"{s['id']}: level id(depth)={level['id']} name={level['name']!r} agency={level.get('agency')!r} "
                f"isIsland={level.get('isIsland')!r} platforms={[p['id'] for p in level['platforms']]}"
            )
        if "exits" in s:
            dropped["exits"].append(f"{s['id']}: {json.dumps(s['exits'])}")
        for p in metro_platforms(s):
            code = p["id"]
            if code in owner:
                lost.append(f"{code}: listed under both {owner[code]} and {sid} in data.json; kept in {owner[code]}")
                continue
            owner[code], heading_of[code] = sid, p["heading"]
            platforms.append(Platform(id=pid(code), heading=p["heading"], name=p.get("name") or None))
            if "terminates" in p:
                dropped["terminates"].append(f"{s['id']} {code}: {p['terminates']}")
            if "stopName" in p:
                stop = snapshot.stops.root.get(pid(code))
                same = "same as 511" if stop and stop.name == p["stopName"] else f"511 says {stop.name!r}" if stop else "not in 511"
                dropped["stopName"].append(f"{s['id']} {code}: {p['stopName']!r} ({same})")
            if p.get("latitude") is not None:
                stop = snapshot.stops.root.get(pid(code))
                if stop:
                    dropped["_coords"].append(
                        f"{metres(p['latitude'], p['longitude'], stop.lat, stop.lon):.1f}|{s['id']} {code}"
                    )
        transfers = []
        for t in s.get("transfers", []):
            if "via" in t:
                dropped["via"].append(f"{s['id']} -> {t['to']}: {t['via']!r}")
            transfers.append(Transfer(to=rid(t["to"]), mode=t["mode"]))
        agencies = []
        for a in s.get("transferAgencies", []):
            if a in AGENCIES:
                agencies.append(AGENCIES[a])
            else:
                agencies_unknown.append(f"{s['id']}: {a!r}")
        station = Station(
            name=s["name"],
            platforms=platforms,
            transfers=transfers,
            transfer_agencies=agencies,
            verified=verified_on,
        )
        if sid != s["id"]:
            station.former_ids = [s["id"]]
            station.note = (
                f"Renamed from the typo '{s['id']}' on {verified_on.isoformat()}. "
                "App Store favourites store the old id; the client must migrate."
            )
        stations[sid] = station
        origin[sid] = "metro"

    # --- bus.json: generated. Joins a data.json station of the same id, else stands alone.
    heading_conflicts, name_conflicts, bus_transfers_dropped, gained = [], [], [], []
    for s in bus["stations"]:
        sid = rid(s["id"])
        fresh = []
        for p in s["platforms"]:
            code = p["id"]
            if code in owner:
                if owner[code] != sid:
                    lost.append(f"{code}: bus.json puts it in {sid}, data.json in {owner[code]}; data.json wins")
                if heading_of[code] != p["heading"]:
                    heading_conflicts.append(f"{code} in {owner[code]}: data.json {heading_of[code]}, bus.json {p['heading']}")
                continue
            owner[code], heading_of[code] = sid, p["heading"]
            fresh.append(Platform(id=pid(code), heading=p["heading"], name=p.get("name") or None))
        transfers = [Transfer(to=rid(t), mode="street") for t in s.get("transferStations", [])]
        agencies = []
        for a in s.get("transferAgencies", []):
            if a in AGENCIES:
                agencies.append(AGENCIES[a])
            else:
                agencies_unknown.append(f"{s['id']} (bus.json): {a!r}")
        if sid in stations:
            station = stations[sid]
            station.platforms += fresh
            origin[sid] = "metro+bus"
            if fresh:
                # The rule is that a change to the platform list clears verified. These
                # bus poles were never checked by hand, so the station is not verified
                # as a whole.
                gained.append(sid)
                station.verified = None
            if s["name"] != station.name:
                name_conflicts.append(f"{sid}: data.json {station.name!r}, bus.json {s['name']!r}")
            if transfers or agencies:
                bus_transfers_dropped.append(f"{sid}: transferStations {s['transferStations']}, transferAgencies {s['transferAgencies']}")
        else:
            stations[sid] = Station(name=s["name"], platforms=fresh, transfers=transfers, transfer_agencies=agencies)
            origin[sid] = "bus"

    # --- one corner split in two by a feed typo: fold the typo station into the real one.
    typo_notes = []
    for old, (new, typo) in typo_merges.items():
        if old not in stations or new not in stations:
            raise SystemExit(f"TYPO_MERGES: {old} -> {new}, but one of them is not a station any more")
        gone, target = stations.pop(old), stations[new]
        origin.pop(old)
        for p in gone.platforms:
            here = snapshot.stops.root.get(p.id)
            if here is None:
                raise SystemExit(f"TYPO_MERGES: {p.id} is not in the 511 snapshot, so the distance cannot be checked")
            dist, near = min(
                (metres(here.lat, here.lon, s.lat, s.lon), q.id)
                for q in target.platforms if (s := snapshot.stops.root.get(q.id))
            )
            if dist > 50:
                raise SystemExit(f"TYPO_MERGES: {p.id} is {dist:.0f} m from {new}; that is not one corner")
            p.note = (
                f"Was the only platform of station '{old}', which existed only because {typo}. "
                f"One corner: {dist:.0f} m from {near}. Merged on {verified_on.isoformat()}; '{old}' is in formerIds."
            )
            target.platforms.append(p)
            owner[upstream_of(p.id)] = new
            typo_notes.append(f"{p.id} ({p.heading}) moved {old} -> {new}: {dist:.0f} m from {near}")
        for t in gone.transfers:
            if t.to != new and all(t.to != u.to for u in target.transfers):
                target.transfers.append(t)
                typo_notes.append(f"{old}'s transfer to {t.to} carried over to {new}")
            else:
                typo_notes.append(f"{old}'s transfer to {t.to} dropped: {new} already has it")
        target.former_ids.append(old)
        target.verified = None
    # Anything that pointed at a typo station now points at the real one, once.
    for sid, st in stations.items():
        seen, kept = set(), []
        for t in st.transfers:
            to = typo_merges[t.to][0] if t.to in typo_merges else t.to
            if to != sid and to not in seen:
                seen.add(to)
                kept.append(t if to == t.to else Transfer(to=to, mode=t.mode, note=t.note))
            if to != t.to:
                typo_notes.append(f"{sid}'s transfer to {t.to} now points at {to}")
        st.transfers = kept

    # --- station-overrides.json: the record of what a person checked on the street.
    by_code ={upstream_of(p.id): (sid, p) for sid, st in stations.items() for p in st.platforms}
    override_notes = []
    for code, entry in real(overrides.get("stations", {})).items():
        hit = by_code.get(code)
        if hit is None:
            override_notes.append(f"{code}: override for {entry['station']} names a stop in no station; note NOT applied")
        elif hit[0] != rid(entry["station"]):
            override_notes.append(f"{code}: override says {entry['station']} but the stop is in {hit[0]}; note NOT applied")
        else:
            hit[1].note = entry["why"]
            override_notes.append(f"{code} in {hit[0]}: note applied")
    for code, heading in real(overrides.get("headings", {})).items():
        heading = heading["heading"] if isinstance(heading, dict) else heading
        hit = by_code.get(code)
        if hit is None:
            override_notes.append(f"{code}: heading override names a stop in no station; NOT applied")
        else:
            override_notes.append(f"{code} in {hit[0]}: heading {hit[1].heading} -> {heading}")
            hit[1].heading = heading
    if not real(overrides.get("headings", {})):
        override_notes.append("headings: none in station-overrides.json")

    ignored = {pid(code): IgnoredStop(note=why) for code, why in real(overrides.get("exclude", {})).items()}

    subways = {
        s["id"]: Subway(name=s["name"], stations=[rid(x) for x in s["stationIds"]]) for s in metro.get("subways", [])
    }

    # --- lines.json: overrides only.
    lines: dict[str, LineOverride] = {}
    for line in metro["lines"]:
        # Always written for data.json's lines, never compared with what the snapshot
        # would derive: these are hand-set, and a future 511 change to its own
        # colours must not silently change Muni's branding in the app.
        override = LineOverride(name=line["name"], color=line["color"])
        if line.get("hidden"):
            override.hidden = True
        lines[pid(line["id"])] = override
    f = lines.setdefault(pid("F"), LineOverride())
    f.mode = "streetcar"
    f.note = "511 calls the F metro, like J-T. It is a heritage streetcar."
    for line, replaced in REPLACEMENTS.items():
        lines.setdefault(pid(line), LineOverride()).replaces = [pid(replaced)]

    curation = Curation(
        stations=StationsFile(subways=subways, stations=stations),
        lines=LinesFile(lines),
        ignored=IgnoredFile(ignored),
    )

    report.section("Conflicts between data.json and bus.json", [
        "Platform codes filed under different stations (data.json wins):",
        *(f"  - {x}" for x in lost or ["none"]),
        "Platform codes with different headings (data.json wins):",
        *(f"  - {x}" for x in heading_conflicts or ["none"]),
        "Same station id, different names (data.json's kept):",
        *(f"  - {x}" for x in name_conflicts or ["none"]),
        "bus.json transfers/agencies on stations whose record is data.json's (dropped, data.json's stand):",
        *(f"  - {x}" for x in bus_transfers_dropped or ["none"]),
        "transferAgencies values with no operator code (dropped, not guessed):",
        *(f"  - {x}" for x in agencies_unknown or ["none"]),
    ])
    report.section("Stations that existed only because of a feed typo (merged)", [f"- {x}" for x in typo_notes])
    report.section("station-overrides.json",[f"- {x}" for x in override_notes] + [f"- exclude: {len(ignored)} entries -> ignored.json"])
    return Seed(curation=curation, origin=origin, gained=gained, dropped=dropped, typo_merges=typo_merges)


# MARK: - Checks


def check(inputs: Inputs, seed: Seed, report: Report, needs_a_person: dict = NEEDS_A_PERSON) -> list[str]:
    """Everything the report asserts. Returns the problems that should fail the run."""
    metro, bus, snapshot = inputs.metro, inputs.bus, inputs.snapshot
    stations = seed.curation.stations.stations
    subways = seed.curation.stations.subways
    ignored = seed.curation.ignored.root
    line_overrides = seed.curation.lines.root
    errors: list[str] = []

    metro_codes = [p["id"] for s in metro["stations"] for p in metro_platforms(s)]
    bus_codes = [p["id"] for s in bus["stations"] for p in s["platforms"]]
    out_codes = [upstream_of(p.id) for st in stations.values() for p in st.platforms]
    metro_ids = {s["id"] for s in metro["stations"]}
    bus_ids = {s["id"] for s in bus["stations"]}
    snap_stops = snapshot.stops.root
    origin = Counter(seed.origin.values())

    # --- counts
    report.section("Counts", [
        "| | in | out |",
        "|---|---|---|",
        f"| data.json stations | {len(metro_ids)} | {origin['metro'] + origin['metro+bus']}, verified: {dict(Counter(str(stations[rid(s['id'])].verified) for s in metro['stations']))} |",
        f"| bus.json-only stations | | {origin['bus']}, verified: {dict(Counter(str(st.verified) for sid, st in stations.items() if seed.origin[sid] == 'bus'))} |",
        f"| bus.json stations | {len(bus_ids)} | {origin['bus']} standalone + {origin['metro+bus']} merged into a data.json station of the same id "
        f"+ {len(seed.typo_merges)} typo stations merged into another bus.json station |",
        f"| stations, total | {len(metro_ids | bus_ids)} distinct ids | {len(stations)} |",
        f"| data.json platforms | {len(metro_codes)} | |",
        f"| bus.json platforms | {len(bus_codes)} ({len(set(bus_codes) & set(metro_codes))} also in data.json) | |",
        f"| platforms, total | {len(set(metro_codes) | set(bus_codes))} distinct codes | {len(out_codes)} |",
        f"| subways | {len(metro.get('subways', []))} | {len(subways)} |",
        f"| transfers | data.json {sum(len(s.get('transfers', [])) for s in metro['stations'])}, bus.json {sum(len(s['transferStations']) for s in bus['stations'])} | {sum(len(s.transfers) for s in stations.values())} |",
        f"| line overrides | data.json lines {len(metro['lines'])} | {len(line_overrides)} |",
        f"| station-overrides exclude | {len(real(inputs.overrides.get('exclude', {})))} | ignored.json {len(ignored)} |",
        f"| 511 stops | {len(snap_stops)} | |",
        f"| 511 lines | {len(snapshot.lines.root)} | |",
        f"| 511 patterns | {sum(len(v) for v in snapshot.patterns.root.values())} ({sum(p.trips for v in snapshot.patterns.root.values() for p in v)} trips) | |",
        "",
        f"verified {sum(1 for s in stations.values() if s.verified)}, unverified {sum(1 for s in stations.values() if not s.verified)}. "
        f"Only data.json stations whose platform list the merge left unchanged are verified. The {len(seed.gained)} that gained "
        "bus.json platforms are not, because those poles were never checked by hand: " + ", ".join(seed.gained),
    ])

    # --- every old platform exactly once
    counts = Counter(out_codes)
    twice = sorted(c for c, n in counts.items() if n > 1)
    missing = sorted((set(metro_codes) | set(bus_codes)) - set(counts))
    new = sorted(set(counts) - set(metro_codes) - set(bus_codes))
    errors += [f"platform {c} in two stations" for c in twice] + [f"platform {c} lost" for c in missing]
    report.section("Every old platform code appears in exactly one station", [
        f"- codes in data.json + bus.json: {len(set(metro_codes) | set(bus_codes))}",
        f"- codes in the output: {len(counts)}, each in exactly one station: {'yes' if not twice else 'NO: ' + ', '.join(twice)}",
        f"- old codes missing from the output: {', '.join(missing) or 'none'}",
        f"- codes in the output that were in neither input: {', '.join(new) or 'none'}",
    ])

    # --- station ids
    old = metro_ids | bus_ids
    refs = [(f"{sid} transfer", t.to) for sid, st in stations.items() for t in st.transfers]
    refs += [(f"subway {k}", x) for k, sub in subways.items() for x in sub.stations]
    refs += [("station id", sid) for sid in stations]
    stale = [f"{where} -> {x}" for where, x in refs if x in RENAMES or x in seed.typo_merges]
    errors += [f"stale reference {x}" for x in stale]
    expected = {rid(x) for x in old} - set(seed.typo_merges)
    lost_ids = sorted(expected - set(stations))
    extra_ids = sorted(set(stations) - expected)
    retired = {**{a: b for a, b in RENAMES.items() if a in old}, **{a: b for a, (b, _) in seed.typo_merges.items() if a in old}}
    unkept = sorted(a for a, b in retired.items() if a not in stations.get(b, Station(name="x", platforms=[])).former_ids)
    errors += [f"station id {x} lost" for x in lost_ids] + [f"station id {x} appeared from nowhere" for x in extra_ids]
    errors += [f"retired id {x} is not in its station's formerIds" for x in unkept]

    # Two live ids that differ only in case are one corner split by a feed typo
    # (bayshoreLEland / bayshoreLeland), and would collide on a case-insensitive
    # filesystem or URL. Fatal, so it cannot come back silently.
    by_folded = defaultdict(list)
    for sid in stations:
        by_folded[sid.lower()].append(sid)
    clashes = sorted(sorted(g) for g in by_folded.values() if len(g) > 1)
    errors += [f"station ids differ only in case: {', '.join(g)}" for g in clashes]
    report.section("Every old station id is preserved, as a live id or a former id", [
        f"- old ids: {len(old)}; output ids: {len(stations)}",
        f"- retired into formerIds: {', '.join(f'{a} -> {b}' for a, b in retired.items())}"
        + (f" (NOT in formerIds: {', '.join(unkept)})" if unkept else ", all kept"),
        f"- old ids missing (other than retired): {', '.join(lost_ids) or 'none'}",
        f"- output ids that were not old ids: {', '.join(extra_ids) or 'none'}",
        f"- references to a retired id outside formerIds (transfers, subways, station ids): {', '.join(stale) or 'none'}",
        f"- live station ids that differ only in case: {'; '.join(', '.join(g) for g in clashes) or 'none'}",
    ])

    # --- platforms not in 511
    by_code = {upstream_of(p.id): (sid, p) for sid, st in stations.items() for p in st.platforms}
    gone = sorted(c for c in counts if pid(c) not in snap_stops)
    report.section(f"Platforms whose code is not in the 511 snapshot ({len(gone)})", [
        f"- {c} in {by_code[c][0]} ({seed.origin[by_code[c][0]]}), "
        f"station's other platforms live: {sum(1 for p in stations[by_code[c][0]].platforms if p.id in snap_stops)}"
        for c in gone
    ])

    # --- 511 stops in no station and not ignored
    serving: dict[str, list[tuple[str, int, bool, bool]]] = defaultdict(list)
    for line, patterns in snapshot.patterns.root.items():
        for p in patterns:
            for i, stop in enumerate(p.stops):
                serving[stop].append((line, p.trips, i == 0, i == len(p.stops) - 1))
    unassigned = sorted(set(snap_stops) - {pid(c) for c in counts} - set(ignored))
    rows = []
    for stop in unassigned:
        uses = serving.get(stop, [])
        trips = sum(t for _, t, _, _ in uses)
        last = sum(t for _, t, _, end in uses if end)
        first = sum(t for _, t, start, _ in uses if start)
        lines = sorted({upstream_of(line) for line, _, _, _ in uses})
        role = " (arrival-only: the last stop of every trip)" if uses and last == trips else " (departure-only: the first stop of every trip)" if uses and first == trips else ""
        rows.append(
            f"- {upstream_of(stop)} {snap_stops[stop].name!r}: lines {', '.join(lines) or 'none'}; "
            f"{trips} trips, first stop of {first}, last stop of {last}{role}"
        )
    report.section(f"511 stops in no station and not ignored ({len(unassigned)})", rows + [
        "",
        f"Plus {len(set(ignored) & set(snap_stops))} stops that are in 511 and deliberately ignored (ignored.json), "
        f"for {len(unassigned) + len(set(ignored) & set(snap_stops))} 511 stops outside every station.",
    ])

    # --- stations with no live platforms
    dead = sorted(sid for sid, st in stations.items() if not any(p.id in snap_stops for p in st.platforms))
    report.section(f"Stations with no live platforms ({len(dead)})", [
        f"- {sid} ({seed.origin[sid]}): {[upstream_of(p.id) for p in stations[sid].platforms]}" for sid in dead
    ])

    # --- needs a person: first in the report, because it is the part to act on
    owner = {p.id: sid for sid, st in stations.items() for p in st.platforms}
    flagged = []
    for platform, (where, evidence) in needs_a_person.items():
        if owner.get(platform) != where:
            errors.append(f"NEEDS_A_PERSON: {platform} expected in {where}, is in {owner.get(platform)}")
            continue
        stop = snap_stops[platform]
        dists = sorted(
            (metres(stop.lat, stop.lon, s.lat, s.lon), q.id, sid)
            for sid in stations for q in stations[sid].platforms
            if q.id != platform and (s := snap_stops.get(q.id)) and metres(stop.lat, stop.lon, s.lat, s.lon) < 150
        )
        flagged.append(
            f"- **{platform}** in `{where}`: {evidence.format(name=stop.name)} Nearest other platforms: "
            + ", ".join(f"{q} in {sid} {d:.0f} m" for d, q, sid in dists[:4])
        )
    report.sections.insert(0, (f"Needs a person ({len(flagged) + len(dead) + len(unassigned)})", [
        "Platforms whose station is in doubt:",
        *flagged,
        "",
        f"Stations with no live platform ({len(dead)}): all their stops are gone from 511's current feed, "
        "so they drop out of the API. Delete, or wait for the stops to return:",
        *(f"- `{sid}`: {', '.join(p.id for p in stations[sid].platforms)}" for sid in dead),
        "",
        f"511 stops in no station and not ignored ({len(unassigned)}): the review queue. Assign each to a station "
        "or ignore it (details in the section below):",
        *(f"- {s} {snap_stops[s].name!r}" for s in unassigned),
    ]))

    # --- integrity: the errors track B's validator will raise, checked here too
    integrity = []
    ids = set(stations)
    formers = Counter(f for st in stations.values() for f in st.former_ids)
    for sid, st in stations.items():
        if not st.platforms:
            integrity.append(f"station-has-no-platforms: {sid}")
        if len(st.platforms) != len({p.id for p in st.platforms}):
            integrity.append(f"platform-listed-twice: {sid}")
        for t in st.transfers:
            if t.to not in ids:
                integrity.append(f"unknown-station: {sid} transfers to {t.to}")
            elif t.mode == "indoor" and not any(b.to == sid and b.mode == "indoor" for b in stations[t.to].transfers):
                integrity.append(f"indoor-transfer-not-reciprocated: {sid} -> {t.to}")
        for p in st.platforms:
            if p.id in ignored:
                integrity.append(f"platform-assigned-and-ignored: {p.id} in {sid}")
    for f, n in formers.items():
        if n > 1 or f in ids:
            integrity.append(f"former-id-collides: {f}")
    for sub_id, sub in subways.items():
        for x in sub.stations:
            if x not in ids:
                integrity.append(f"unknown-station: subway {sub_id} lists {x}")
    for line_id, o in line_overrides.items():
        for r in o.replaces:
            if r not in snapshot.lines.root:
                integrity.append(f"unknown-line: {line_id} replaces {r}")
    errors += integrity
    warnings = [f"unknown-line-override: {x} (511 has no such line)" for x in sorted(set(line_overrides) - set(snapshot.lines.root))]
    one_way = sum(
        1 for sid, st in stations.items() for t in st.transfers
        if t.mode == "street" and t.to in stations and not any(b.to == sid for b in stations[t.to].transfers)
    )
    report.section("Referential integrity (the PLAN's error codes)", [
        f"- transfers checked: {sum(len(s.transfers) for s in stations.values())} "
        f"({sum(1 for s in stations.values() for t in s.transfers if t.mode == 'indoor')} indoor, all reciprocated unless listed below); "
        f"{one_way} street transfers are one-way, deliberately (tools/README.md)",
        f"- subway entries checked: {sum(len(s.stations) for s in subways.values())}",
        f"- former ids: {dict(formers)}",
        f"- errors: {len(integrity)}",
        *(f"  - {x}" for x in integrity),
        f"- warnings: {len(warnings)}",
        *(f"  - {x}" for x in warnings),
    ])

    # --- lines
    rows = ["| line | curated name | curated colour | hidden | mode | replaces | 511 short / long | 511 colour |", "|---|---|---|---|---|---|---|---|"]
    for line_id, o in sorted(line_overrides.items()):
        snap = snapshot.lines.root.get(line_id)
        rows.append(
            f"| {line_id} | {o.name or ''} | {o.color or ''} | {'yes' if o.hidden else ''} | {o.mode or ''} | {', '.join(o.replaces)} | "
            + (f"{snap.short_name} / {snap.long_name} | {snap.color} |" if snap else "**not in 511** | |")
        )
    rows += ["", "`SF:S` (S Shuttle, hidden) is data.json's only line 511 does not have. Its override is kept on purpose; "
             "the validator's `unknown-line-override` warning for it is expected."]
    report.section("lines.json", rows)

    # --- dropped from data.json
    dropped = dict(seed.dropped)
    coords = sorted((float(x.split("|")[0]), x.split("|")[1]) for x in dropped.pop("_coords", []))
    far = [f"  - {name}: {d:.1f} m" for d, name in coords if d > 1]
    body = [
        "Coordinates, per-platform `lines` and per-station `lines` are dropped by design: they are derived "
        "from the 511 snapshot at load time (PLAN.md). Line `shortName` and `stationIds` likewise.",
        f"- platform coordinates: {len(coords)} compared with 511's; max difference {coords[-1][0] if coords else 0:.1f} m; "
        f"{len(far)} differ by more than 1 m" + (":" if far else ""),
        *far,
        f"- station latitude/longitude: {len(metro['stations'])} dropped "
        f"({sum(1 for s in metro['stations'] if s.get('latitude') is None)} were null)",
    ]
    for key, title in (
        ("levels", "levels (id is the depth; flattened into `platforms` in this order)"),
        ("exits", "exits"),
        ("terminates", "terminates (now derived per trip from realtime)"),
        ("stopName", "stopName (511's name now comes from the snapshot)"),
        ("via", "transfer `via`"),
    ):
        items = dropped.get(key, [])
        body += ["", f"### {title}: {len(items)}", *(f"- {x}" for x in items)]
    island = Counter(x.split("isIsland=")[1].split(" ")[0] for x in dropped.get("levels", []))
    body += ["", f"isIsland values across all levels: {dict(island)}"]
    report.section("Everything dropped from data.json", body)
    return errors


# MARK: - Main


def round_trip(root: Path) -> list[str]:
    """Read what was written and write it back: nothing may change."""
    before = {p: p.read_bytes() for p in root.rglob("*.json")}
    changed = files.write_curation(root, files.read_curation(root))
    for operator in files.snapshot_operators(root):
        changed += files.write_snapshot(root, files.read_snapshot(root, operator))
    after = {p: p.read_bytes() for p in root.rglob("*.json")}
    return changed + [str(p) for p in before if before[p] != after.get(p)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True, help="sf-transit tree to create (must not exist, or be empty)")
    ap.add_argument("--date", type=date.fromisoformat, required=True, help="the verified date for hand-curated stations")
    ap.add_argument("--report", type=Path, default=API / ".local" / "seed-report.md")
    ap.add_argument("--upstream", type=Path, required=True, help="directory holding gtfs-511-SF.zip, lines-511-SF.json, gtfs.headers")
    ap.add_argument("--metro-ref", default="origin/claude/beautiful-cerf-23u8yd")
    ap.add_argument("--bus", type=Path, default=REPO / "appdata" / "bus.json")
    ap.add_argument("--overrides", type=Path, default=REPO / "tools" / "station-overrides.json")
    ap.add_argument("--replace", action="store_true", help="delete --out first if it exists")
    args = ap.parse_args(argv)

    if args.report.resolve().is_relative_to(args.out.resolve()):
        ap.error("--report must be outside --out: it is not part of sf-transit")
    if args.out.exists() and any(args.out.iterdir()) and not args.replace:
        ap.error(f"{args.out} is not empty; pass --replace to start it fresh")

    inputs = load(args)
    report = Report()
    seed = convert(inputs, args.date, report)
    errors = check(inputs, seed, report)

    # A seed that fails its own checks writes the report and nothing else, and leaves
    # any existing tree alone: a half-right tree is worse than none.
    written, changed = [], []
    if not errors:
        if args.out.exists():
            shutil.rmtree(args.out)
        written = files.write_curation(args.out, seed.curation) + files.write_snapshot(args.out, inputs.snapshot)
        changed = round_trip(args.out)
        errors += [f"round trip changed {x}" for x in changed]

    head = [
        "# sf-transit seed: reconciliation report",
        "",
        f"Generated {datetime.now(UTC).replace(microsecond=0).isoformat()} by api/scripts/seed.py.",
        "",
        "## Inputs",
        "",
        *(f"- {k}: {v}" for k, v in inputs.sources.items()),
        f"- verified date: {args.date.isoformat()}",
        "",
        "## Output",
        "",
        f"- tree: {args.out.resolve()}" + ("" if written else " (NOT written: the checks failed)"),
        *(f"- wrote {x}" for x in written),
        f"- round trip (read_curation/read_snapshot, written back): "
        + ("not run" if not written else "byte-identical, nothing rewritten" if not changed else "CHANGED " + ", ".join(changed)),
        f"- problems: {len(errors)}",
        *(f"  - {x}" for x in errors),
        "",
    ]
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text("\n".join(head) + "\n" + report.render())
    print(f"wrote {args.out if written else 'NO tree'} and {args.report}; {len(errors)} problem(s)")
    for x in errors:
        print("  " + x)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
