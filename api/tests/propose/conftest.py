"""Inputs for comparing the proposer against the generator it was ported from.

These tests need real data that is not in the repo's fixtures: 511's GTFS zip for
SF (already downloaded, never fetched here) and the hand-curated metro file on
the ``claude/beautiful-cerf-23u8yd`` branch. Without them the differential tests
skip rather than fail.

``tools/build_bus_data.py`` is imported read-only and only its pure functions
are called. Its ``main()`` writes ``appdata/bus.json`` and ``station-ids.json``,
so it is never run from here.
"""

import collections
import importlib.util
import json
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.models.curation import Curation, IgnoredFile, IgnoredStop, Platform, Station, StationsFile, Subway
from app.models.snapshot import Pattern, Snapshot, SnapshotLine, SnapshotLines, SnapshotMeta, SnapshotPatterns, SnapshotStop, SnapshotStops

API = Path(__file__).resolve().parents[2]
REPO = API.parent
UPSTREAM_ZIP = Path("api") / ".local" / "upstream" / "gtfs-511-SF.zip"
CERF = "origin/claude/beautiful-cerf-23u8yd"


def ref(code):
    return f"SF:{code}"


def code(pid):
    return pid.split(":", 1)[1]


@pytest.fixture(scope="session")
def original():
    spec = importlib.util.spec_from_file_location("build_bus_data", REPO / "tools" / "build_bus_data.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _checkouts():
    """This checkout, then the main one: a git worktree has no ``.local`` of its own."""
    yield REPO
    try:
        common = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--path-format=absolute",
                                 "--git-common-dir"], capture_output=True, text=True, check=True).stdout
        yield Path(common.strip()).parent
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass


@pytest.fixture(scope="session")
def gtfs(original):
    """``(routes, stops, patterns)`` for every route, parsed once for the session."""
    path = next((root / UPSTREAM_ZIP for root in _checkouts() if (root / UPSTREAM_ZIP).exists()), None)
    if path is None:
        pytest.skip("511 GTFS zip for SF is not downloaded")
    return original.read_gtfs(str(path))


@pytest.fixture(scope="session")
def snapshot(gtfs):
    """The same GTFS as a Snapshot. A minimal converter; track A owns the real one.

    Headsigns are left blank: the proposer never reads them."""
    routes, stops, patterns = gtfs
    by_line = collections.defaultdict(list)
    for (route, direction, seq), trips in patterns.items():
        by_line[ref(route)].append(Pattern(direction=int(direction), headsign="", trips=trips,
                                           stops=[ref(stops[s]["stop_code"]) for s in seq]))
    return Snapshot(
        meta=SnapshotMeta(operator="SF", service_from=date(2026, 1, 1), service_to=date(2026, 12, 31),
                          fetched_at=datetime(2026, 9, 22, tzinfo=timezone.utc), source="test", sha256="0" * 64),
        stops=SnapshotStops({ref(s["stop_code"]): SnapshotStop(name=s["stop_name"], lat=float(s["stop_lat"]),
                                                               lon=float(s["stop_lon"]))
                             for s in stops.values()}),
        lines=SnapshotLines({ref(r): SnapshotLine(short_name=v["route_short_name"], long_name=v["route_long_name"],
                                                  mode="bus", route_type=int(v["route_type"]))
                             for r, v in routes.items()}),
        patterns=SnapshotPatterns(by_line),
    )


@pytest.fixture(scope="session")
def cerf():
    """The hand-curated metro data.json, platforms flattened out of ``levels``."""
    try:
        text = subprocess.run(["git", "-C", str(REPO), "show", f"{CERF}:appdata/data.json"],
                              capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip(f"{CERF} is not available")
    doc = json.loads(text)
    for st in doc["stations"]:
        if "levels" in st:
            st["platforms"] = [p for lv in st["levels"] for p in lv["platforms"]]
    return doc


@pytest.fixture(scope="session")
def underground():
    """Station ids that were ``kind: underground``. cerf's data.json dropped ``kind``
    (its underground stations are the ones with ``levels``); master's still has it."""
    doc = json.loads((REPO / "appdata" / "data.json").read_text())
    return {st["id"] for st in doc["stations"] if st["kind"] == "underground"}


@pytest.fixture(scope="session")
def overrides():
    doc = json.loads((REPO / "tools" / "station-overrides.json").read_text())
    real = lambda m: {k: v for k, v in m.items() if not k.startswith("//")}
    return {
        "stations": {k: v["station"] for k, v in real(doc.get("stations", {})).items()},
        "headings": {k: v["heading"] for k, v in real(doc.get("headings", {})).items()},
        "exclude": real(doc.get("exclude", {})),
    }


@pytest.fixture(scope="session")
def bus():
    return json.loads((REPO / "appdata" / "bus.json").read_text())


@pytest.fixture(scope="session")
def metro_for_original(cerf, underground):
    """cerf's data.json in the shape the old generator reads: flat platforms, a
    ``kind``, and a station coordinate (null in cerf for the underground ones, which
    the generator only uses for transfer links)."""
    stations = []
    for st in cerf["stations"]:
        ps = st["platforms"]
        stations.append({
            **st,
            "kind": "underground" if st["id"] in underground else "streetLevel",
            "latitude": st["latitude"] if st["latitude"] is not None else sum(p["latitude"] for p in ps) / len(ps),
            "longitude": st["longitude"] if st["longitude"] is not None else sum(p["longitude"] for p in ps) / len(ps),
        })
    return {"lines": cerf["lines"], "subways": cerf["subways"], "stations": stations}


def to_curation(docs, excluded):
    """data.json-shaped documents to a Curation, folded in the order given with the
    app's loader rule: the first document to name a station keeps its name, later
    ones only add platforms it does not have yet. A minimal converter; the real seed
    is track A's."""
    stations: dict[str, dict] = {}
    subways = {}
    for doc in docs:
        for sub in doc.get("subways", []):
            subways[sub["id"]] = Subway(name=sub["name"], stations=sub["stationIds"])
        for st in doc["stations"]:
            cur = stations.setdefault(st["id"], {"name": st["name"], "platforms": {}})
            for p in st["platforms"]:
                cur["platforms"].setdefault(p["id"], p["heading"])
    return Curation(
        stations=StationsFile(
            subways=subways,
            stations={sid: Station(name=v["name"], platforms=[Platform(id=ref(c), heading=h)
                                                              for c, h in v["platforms"].items()])
                      for sid, v in stations.items()}),
        ignored=IgnoredFile({ref(c): IgnoredStop(note=why) for c, why in excluded.items()}),
    )
