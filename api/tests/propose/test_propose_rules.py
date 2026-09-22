"""The proposer's rules on small hand-built data. They need no downloads, and they
document the traps the differential tests check at full scale."""

import math
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.ingest.propose import Proposal, compute_headings, propose, stop_patterns
from app.models import files
from app.models.curation import Curation, IgnoredFile, IgnoredStop, Platform, Station, StationsFile, Subway
from app.models.snapshot import Pattern, Snapshot, SnapshotMeta, SnapshotPatterns, SnapshotStop, SnapshotStops

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "transit"


def at(x, y):
    """Metres east and north of a point in the Mission, as (lat, lon)."""
    return 37.76 + y / 110540.0, -122.43 + x / (111320.0 * math.cos(math.radians(37.76)))


def snapshot(stops, patterns):
    """``stops``: {code: (name, x, y)}; ``patterns``: {line: [[code, ...], ...]}."""
    return Snapshot(
        meta=SnapshotMeta(operator="SF", service_from=date(2026, 1, 1), service_to=date(2026, 12, 31),
                          fetched_at=datetime(2026, 9, 22, tzinfo=timezone.utc), source="test", sha256="0" * 64),
        stops=SnapshotStops({f"SF:{c}": SnapshotStop(name=n, lat=at(x, y)[0], lon=at(x, y)[1])
                             for c, (n, x, y) in stops.items()}),
        lines={},
        patterns=SnapshotPatterns({f"SF:{line}": [Pattern(direction=0, headsign="x", trips=10,
                                                          stops=[f"SF:{c}" for c in seq]) for seq in seqs]
                                   for line, seqs in patterns.items()}),
    )


def curation(stations, subways=None, ignored=()):
    return Curation(
        stations=StationsFile(
            subways={k: Subway(name=k, stations=v) for k, v in (subways or {}).items()},
            stations={sid: Station(name=name, former_ids=former,
                                   platforms=[Platform(id=f"SF:{c}", heading="northbound") for c in codes])
                      for sid, (name, codes, former) in stations.items()}),
        ignored=IgnoredFile({f"SF:{c}": IgnoredStop(note="test") for c in ignored}),
    )


# MARK: - Headings


def test_a_stop_before_a_turn_takes_the_street_it_is_on():
    # The 22 at 16th & Church is still running west on 16th and only turns onto
    # Church after leaving. Travel through the stop says north.
    snap = snapshot({
        "1": ("16th St & Guerrero St", 0, 0),
        "2": ("16th St & Dolores St", -100, 0),
        "3": ("16th St & Church St", -200, 0),
        "4": ("Church St & 15th St", -200, 200),
        "5": ("Church St & 14th St", -200, 400),
    }, {"22": [["1", "2", "3", "4", "5"]]})
    head, snapped = compute_headings(stop_patterns(snap), snap.stops.root)
    assert head["SF:3"] == "westbound"
    assert snapped == {"SF:3": "northbound"}


def test_a_street_on_the_rotated_grid_is_not_snapped():
    # Consecutive 3rd St stops in SoMa differ by dx=245, dy=-240. A run that is not
    # lopsided 2:1 is left alone, and travel decides.
    snap = snapshot({
        "1": ("3rd St & Folsom St", 0, 0),
        "2": ("3rd St & Harrison St", 245, -240),
        "3": ("3rd St & Bryant St", 490, -480),
    }, {"45": [["1", "2", "3"]]})
    _, snapped = compute_headings(stop_patterns(snap), snap.stops.root)
    assert snapped == {}


# MARK: - Stations


def test_new_stops_at_one_corner_share_a_new_station_with_a_fresh_id():
    snap = snapshot({
        "1": ("Geary Blvd & 33rd Ave", 0, 0),
        "2": ("Geary Blvd & 33rd Ave", 20, 15),
        "3": ("Geary Blvd & 32nd Ave", 100, 0),
    }, {"38": [["3", "1"], ["2", "3"]]})
    # 'geary33' is taken live and 'geary332' as a former id, so the next is 'geary333'
    cur = curation({"geary33": ("Somewhere Else", ["3"], []), "other": ("Other", [], ["geary332"])})
    out = propose(snap, cur, ["SF:1", "SF:2"])
    assert [p.new_station.id for p in out] == ["geary333", "geary333"]
    assert out[0].new_station.name == "Geary & 33rd"
    assert out[0].station is None


def test_a_stop_joins_the_station_its_intersection_is_listed_under():
    snap = snapshot({
        "1": ("Church St & Market St", 0, 0),
        "2": ("Church St & Market St", 40, 30),
    }, {"22": [["1", "2"]]})
    cur = curation({"churchMarket": ("Church & Market", ["1"], [])})
    (p,) = propose(snap, cur, ["SF:2"])
    assert p.station == "churchMarket" and p.new_station is None
    assert "SF:1" in p.reason


def test_same_corner_joins_but_an_underground_station_is_never_merged_into():
    # 'Church' is a subset of 'Church St & 14th St', so without the underground
    # rule the corner match would file a street stop under the subway station.
    snap = snapshot({
        "1": ("Metro Church Station", 0, 0),
        "2": ("Church St & 14th St", 30, 0),
        "3": ("Duboce Ave & Church St", 0, 400),
        "4": ("Church St & Duboce Ave", 30, 400),
    }, {"J": [["1", "3"]], "37": [["2", "4"]]})
    cur = curation({"church": ("Church", ["1"], []), "churchDuboce": ("Church & Duboce", ["3"], [])},
                   subways={"market": ["church"]})
    by = {p.platform: p for p in propose(snap, cur, ["SF:2", "SF:4"])}
    assert by["SF:2"].new_station.id == "church14"
    assert by["SF:4"].station == "churchDuboce"


def test_assigned_and_ignored_stops_are_skipped_and_unknown_ones_refused():
    snap = files.read_snapshot(FIXTURE, "SF")
    cur = files.read_curation(FIXTURE)
    out = propose(snap, cur, list(snap.stops.root))
    # everything in the fixture is assigned or ignored (SF:13510), bar one
    assert [p.platform for p in out] == ["SF:15418"]
    assert out[0].new_station.name == "Balboa Park BART/Mezzanine Level"
    # no fixture pattern stops there, so there is no direction of travel
    assert out[0].heading is None
    assert out[0].model_dump()["newStation"]["id"] == "balboaParkBartmezzanineLevel"
    with pytest.raises(ValueError):
        propose(snap, cur, ["SF:99999"])


def test_proposal_is_camel_case_on_the_wire():
    p = Proposal(platform="SF:1", heading="northbound", station="x", reason="r")
    assert set(p.model_dump()) == {"platform", "heading", "station", "newStation", "reason"}
