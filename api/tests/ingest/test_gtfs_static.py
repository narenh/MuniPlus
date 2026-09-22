"""The static ingest against a tiny synthetic feed, built here so every rule has a
case that would fail if the rule were dropped."""

import hashlib
import json
import zipfile
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.ingest.gtfs_static import SOURCE, GtfsError, build_snapshot, normalise_color
from app.models import files

FETCHED = datetime(2026, 9, 22, 23, 12, 36, tzinfo=UTC)

STOPS = """\
stop_id,stop_code,stop_name,stop_lat,stop_lon,location_type,parent_station
100,100,Market St & 1st St,37.79,-122.39,,
101,101,Market St & 2nd St,37.789,-122.40,0,
102,102,Market St & 3rd St,37.788,-122.401,,
103,103,Market St & 4th St,37.787,-122.402,,
900,900,Embarcadero Station,37.793,-122.396,1,
901,901,Embarcadero Entrance,37.7931,-122.3961,2,900
"""

ROUTES = """\
route_id,agency_id,route_short_name,route_long_name,route_type,route_color,route_text_color
F,SF,F,MARKET & WHARVES,0,b49a36,000000
5,SF,5,FULTON,3,#005b95,
"""

# Trips 1-4 run one sequence (100, 101, 102) in direction 0, with headsigns split
# 2-2 so the tie-break is exercised. Trip 5 skips 101. Trip 6 is the same stops as
# 1-4 but direction 1, which must be its own pattern.
TRIPS = """\
route_id,service_id,trip_id,trip_headsign,direction_id
F,wk,t1,Fisherman's Wharf,0
F,wk,t2,Wharf,0
F,wk,t3,Wharf,0
F,wk,t4,Fisherman's Wharf,0
F,wk,t5,Wharf,0
F,wk,t6,Castro,1
5,wk,t7,Ocean Beach,0
"""


def stop_times() -> str:
    rows = ["trip_id,arrival_time,departure_time,stop_id,stop_sequence"]
    for trip in ("t1", "t2", "t3", "t4", "t6"):
        rows += [f"{trip},8:00:00,8:00:00,100,1", f"{trip},8:01:00,8:01:00,101,2", f"{trip},8:02:00,8:02:00,102,3"]
    rows += ["t5,9:00:00,9:00:00,100,1", "t5,9:02:00,9:02:00,102,3"]
    # Out of order in the file, and sequence numbers that sort wrongly as strings
    # ("10" < "9"): the pattern must still be 103, 102, 101.
    rows += ["t7,7:05:00,7:05:00,101,10", "t7,7:00:00,7:00:00,103,2", "t7,7:03:00,7:03:00,102,9"]
    return "\n".join(rows) + "\n"


FEED_INFO = "feed_publisher_name,feed_start_date,feed_end_date\n511 SF Bay,20260829,20270115"
CALENDAR = "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\nwk,1,1,1,1,1,0,0,20260901,20261201\n"
CALENDAR_DATES = "service_id,date,exception_type\nwk,20260831,1\nwk,20261225,2\n"

LINES = [
    {"Id": "F", "Name": "MARKET & WHARVES", "TransportMode": "metro", "PublicCode": "F"},
    {"Id": "5", "Name": "FULTON", "TransportMode": "bus", "PublicCode": "5"},
    {"Id": "PM", "Name": "POWELL-MASON", "TransportMode": "cableway", "PublicCode": "PM"},
]


def make_feed(tmp_path: Path, *, lines=LINES, bom=True, **tables: str | None) -> tuple[Path, Path]:
    contents = {
        "stops.txt": STOPS,
        "routes.txt": ROUTES,
        "trips.txt": TRIPS,
        "stop_times.txt": stop_times(),
        "feed_info.txt": FEED_INFO,
        "calendar.txt": CALENDAR,
        "calendar_dates.txt": CALENDAR_DATES,
    }
    contents.update({f"{name}.txt": text for name, text in tables.items()})
    zip_path = tmp_path / "gtfs.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name, text in contents.items():
            if text is not None:
                zf.writestr(name, text)
    lines_path = tmp_path / "lines.json"
    # 511 sends a byte order mark; the real file starts with one.
    lines_path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + json.dumps(lines).encode())
    return zip_path, lines_path


def build(tmp_path, **kwargs):
    return build_snapshot(*make_feed(tmp_path, **kwargs), "SF", FETCHED)


# MARK: - Stops


def test_only_stops_are_kept(tmp_path):
    snapshot = build(tmp_path)
    assert sorted(snapshot.stops.root) == ["SF:100", "SF:101", "SF:102", "SF:103"]
    stop = snapshot.stops.root["SF:101"]
    assert (stop.name, stop.lat, stop.lon) == ("Market St & 2nd St", 37.789, -122.40)


# MARK: - Lines


def test_lines(tmp_path):
    lines = build(tmp_path).lines.root
    # PM is in /transit/lines but has no route in the zip: nothing to serve.
    assert sorted(lines) == ["SF:5", "SF:F"]
    f = lines["SF:F"]
    assert (f.short_name, f.long_name, f.mode, f.route_type) == ("F", "MARKET & WHARVES", "metro", 0)
    assert (f.color, f.text_color) == ("#B49A36", "#000000")
    assert (lines["SF:5"].color, lines["SF:5"].text_color, lines["SF:5"].mode) == ("#005B95", None, "bus")


def test_lines_json_without_bom(tmp_path):
    assert build(tmp_path, bom=False).lines.root["SF:F"].mode == "metro"


def test_a_line_511_gives_no_mode_for_is_an_error(tmp_path):
    with pytest.raises(GtfsError, match="route 5 has no TransportMode"):
        build(tmp_path, lines=[line for line in LINES if line["Id"] != "5"])


@pytest.mark.parametrize(("raw", "out"), [("b49a36", "#B49A36"), ("#005B95", "#005B95"), (" 666666 ", "#666666"), ("", None), (None, None)])
def test_colours(raw, out):
    assert normalise_color(raw) == out


@pytest.mark.parametrize("raw", ["red", "12345", "1234567", "GGGGGG"])
def test_bad_colours(raw):
    with pytest.raises(GtfsError):
        normalise_color(raw)


# MARK: - Patterns


def test_patterns(tmp_path):
    patterns = build(tmp_path).patterns.root
    f = [(p.direction, p.trips, p.headsign, p.stops) for p in patterns["SF:F"]]
    assert f == [
        (0, 4, "Fisherman's Wharf", ["SF:100", "SF:101", "SF:102"]),
        (0, 1, "Wharf", ["SF:100", "SF:102"]),
        (1, 1, "Castro", ["SF:100", "SF:101", "SF:102"]),
    ]
    assert [p.stops for p in patterns["SF:5"]] == [["SF:103", "SF:102", "SF:101"]]


def test_headsign_is_the_most_common(tmp_path):
    trips = TRIPS.replace("t4,Fisherman's Wharf", "t4,Wharf")
    assert build(tmp_path, trips=trips).patterns.root["SF:F"][0].headsign == "Wharf"


def test_unknown_stop_is_an_error(tmp_path):
    # 900 is a station (location_type 1): not somewhere a trip can stop.
    with pytest.raises(GtfsError, match="not a stop"):
        build(tmp_path, stop_times=stop_times() + "t1,8:03:00,8:03:00,900,4\n")


def test_trip_on_unknown_route_is_an_error(tmp_path):
    with pytest.raises(GtfsError, match="routes.txt does not list"):
        build(tmp_path, trips=TRIPS + "X,wk,t8,Nowhere,0\n")


def test_trip_without_direction_is_an_error(tmp_path):
    with pytest.raises(GtfsError, match="direction_id"):
        build(tmp_path, trips=TRIPS.replace("t6,Castro,1", "t6,Castro,"))


def test_repeated_stop_sequence_is_an_error(tmp_path):
    with pytest.raises(GtfsError, match="repeats a stop_sequence"):
        build(tmp_path, stop_times=stop_times() + "t1,8:03:00,8:03:00,103,3\n")


# MARK: - Meta


def test_meta(tmp_path):
    zip_path, lines_path = make_feed(tmp_path)
    meta = build_snapshot(zip_path, lines_path, "SF", FETCHED).meta
    assert (meta.operator, meta.service_from, meta.service_to) == ("SF", date(2026, 8, 29), date(2027, 1, 15))
    assert meta.sha256 == hashlib.sha256(zip_path.read_bytes()).hexdigest()
    assert meta.source == SOURCE
    assert meta.fetched_at == FETCHED


def test_service_period_falls_back_to_the_calendar(tmp_path):
    # calendar.txt runs 09-01..12-01; calendar_dates adds 08-31 and removes 12-25,
    # and a removal must not stretch the period.
    meta = build(tmp_path, feed_info=None).meta
    assert (meta.service_from, meta.service_to) == (date(2026, 8, 31), date(2026, 12, 1))


def test_service_period_with_blank_feed_info_dates(tmp_path):
    meta = build(tmp_path, feed_info="feed_publisher_name,feed_start_date,feed_end_date\n511 SF Bay,,\n").meta
    assert meta.service_from == date(2026, 8, 31)


def test_fetched_at_must_be_aware(tmp_path):
    with pytest.raises(GtfsError):
        build_snapshot(*make_feed(tmp_path), "SF", datetime(2026, 9, 22, 23, 12, 36))


def test_fetched_at_is_written_as_utc(tmp_path):
    pacific = timezone(timedelta(hours=-7))
    snapshot = build_snapshot(*make_feed(tmp_path), "SF", FETCHED.astimezone(pacific))
    root = tmp_path / "transit"
    files.write_snapshot(root, snapshot)
    assert '"fetchedAt": "2026-09-22T23:12:36Z"' in (root / "snapshot/SF/meta.json").read_text()


# MARK: - Files


def test_round_trip_is_byte_identical(tmp_path):
    root = tmp_path / "transit"
    written = files.write_snapshot(root, build(tmp_path))
    assert len(written) == 4
    before = {p: p.read_bytes() for p in root.rglob("*.json")}
    assert files.write_snapshot(root, files.read_snapshot(root, "SF")) == []
    assert {p: p.read_bytes() for p in root.rglob("*.json")} == before


def test_rebuilding_the_same_zip_is_byte_identical(tmp_path):
    zip_path, lines_path = make_feed(tmp_path)
    a = files.dumps(build_snapshot(zip_path, lines_path, "SF", FETCHED).patterns)
    b = files.dumps(build_snapshot(zip_path, lines_path, "SF", FETCHED).patterns)
    assert a == b
