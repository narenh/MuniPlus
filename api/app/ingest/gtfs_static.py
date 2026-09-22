"""One operator's snapshot, built from 511's GTFS zip and its ``/transit/lines``.

This is the only place GTFS vocabulary survives: routes become lines here, and
stops become platform ids (``SF:16992``). Everything downstream reads the
``Snapshot`` this returns.

The build is strict. A route 511 gives no mode for, a trip on an unknown route or
a stop time at an unknown stop raises ``GtfsError`` instead of being guessed
around or dropped: a snapshot is committed to sf-transit and served as fact, so a
feed that has changed shape needs a person to look at it, not a silent gap.
"""

import csv
import hashlib
import io
import json
import re
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

from app.models.ids import ref
from app.models.snapshot import (
    Pattern,
    Snapshot,
    SnapshotLine,
    SnapshotLines,
    SnapshotMeta,
    SnapshotPatterns,
    SnapshotStop,
    SnapshotStops,
)

SOURCE = "511 /transit/datafeeds + /transit/lines"


class GtfsError(ValueError):
    """The feed is not what this code was written against."""


def build_snapshot(gtfs_zip: Path, lines_json: Path, operator: str, fetched_at: datetime) -> Snapshot:
    if fetched_at.tzinfo is None:
        # Written as UTC to meta.json and compared against realtime epoch times; a
        # naive time would silently be read as the server's local zone.
        raise GtfsError("fetched_at must be timezone-aware")

    modes = read_modes(lines_json)
    with zipfile.ZipFile(gtfs_zip) as zf:
        stops = _stops(zf, operator)
        routes = {row["route_id"]: row for row in _rows(zf, "routes.txt")}
        lines = _lines(routes, modes, operator)
        patterns = _patterns(zf, routes, stops, operator)
        service_from, service_to = _service_period(zf)

    meta = SnapshotMeta(
        operator=operator,
        service_from=service_from,
        service_to=service_to,
        # Always UTC on disk, so two refreshes made in different zones differ in
        # meta.json only when the instant does.
        fetched_at=fetched_at.astimezone(UTC),
        source=SOURCE,
        sha256=hashlib.sha256(gtfs_zip.read_bytes()).hexdigest(),
    )
    return Snapshot(meta=meta, stops=stops, lines=lines, patterns=patterns)


# MARK: - /transit/lines


def read_modes(lines_json: Path) -> dict[str, str]:
    """511 line ``Id`` -> ``TransportMode``, verbatim.

    511 serves this JSON with a UTF-8 byte order mark, which ``json.loads`` refuses,
    hence ``utf-8-sig``.
    """
    doc = json.loads(lines_json.read_bytes().decode("utf-8-sig"))
    if not isinstance(doc, list):
        raise GtfsError(f"{lines_json.name}: expected a list of lines, got {type(doc).__name__}")
    modes = {}
    for line in doc:
        line_id, mode = line.get("Id"), line.get("TransportMode")
        if not line_id or not mode:
            raise GtfsError(f"{lines_json.name}: line without Id or TransportMode: {line!r}")
        if modes.setdefault(line_id, mode) != mode:
            raise GtfsError(f"{lines_json.name}: line {line_id} listed with two modes")
    return modes


# MARK: - Tables


def _rows(zf: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    """Stream one table. ``utf-8-sig`` because GTFS producers commonly write a BOM,
    which would otherwise end up inside the first column's name."""
    with zf.open(name) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))


def _has(zf: zipfile.ZipFile, name: str) -> bool:
    return name in zf.namelist()


def _stops(zf: zipfile.ZipFile, operator: str) -> SnapshotStops:
    # location_type 1-4 are stations, entrances, generic nodes and boarding areas:
    # places, not stops a vehicle serves. 511's SF feed has none (all 3,240 rows are
    # blank), but other operators' feeds do.
    out = {}
    for row in _rows(zf, "stops.txt"):
        if row.get("location_type", "") not in ("", "0"):
            continue
        out[ref(operator, row["stop_id"])] = SnapshotStop(
            name=row["stop_name"],
            lat=float(row["stop_lat"]),
            lon=float(row["stop_lon"]),
        )
    return SnapshotStops(out)


# MARK: - Lines


_HEX = re.compile(r"^[0-9A-F]{6}$")


def normalise_color(value: str | None) -> str | None:
    """GTFS writes ``942d83`` or ``942D83`` with no hash; the contract wants ``#942D83``.
    Blank means the feed states no colour, which is not the same as black."""
    value = (value or "").strip().lstrip("#").upper()
    if not value:
        return None
    if not _HEX.match(value):
        raise GtfsError(f"not a colour: {value!r}")
    return f"#{value}"


def _lines(routes: dict[str, dict[str, str]], modes: dict[str, str], operator: str) -> SnapshotLines:
    out = {}
    for route_id, row in routes.items():
        # The zip has only route_type, which cannot tell the F from the J (both 0).
        # A route missing from /transit/lines means the two calls disagree about what
        # exists, and guessing a mode from route_type would hide exactly that.
        if route_id not in modes:
            raise GtfsError(f"route {route_id} has no TransportMode in /transit/lines")
        out[ref(operator, route_id)] = SnapshotLine(
            short_name=row.get("route_short_name", ""),
            long_name=row.get("route_long_name", ""),
            mode=modes[route_id],
            route_type=int(row["route_type"]),
            color=normalise_color(row.get("route_color")),
            text_color=normalise_color(row.get("route_text_color")),
        )
    # Lines /transit/lines has and the zip does not are ignored: with no trips there
    # is nothing to serve, and the zip is what defines the service period.
    return SnapshotLines(out)


# MARK: - Patterns


def _patterns(
    zf: zipfile.ZipFile,
    routes: dict[str, dict[str, str]],
    stops: SnapshotStops,
    operator: str,
) -> SnapshotPatterns:
    trips: dict[str, tuple[str, int, str]] = {}
    for row in _rows(zf, "trips.txt"):
        route_id = row["route_id"]
        if route_id not in routes:
            raise GtfsError(f"trip {row['trip_id']} is on route {route_id}, which routes.txt does not list")
        # The realtime feeds key directions on this same 0/1, so a trip without one
        # could never be matched to a pattern. 511's SF trips all have it (34,668).
        direction = row.get("direction_id", "")
        if direction not in ("0", "1"):
            raise GtfsError(f"trip {row['trip_id']} has direction_id {direction!r}")
        trips[row["trip_id"]] = (route_id, int(direction), row.get("trip_headsign", ""))

    # stop_times.txt is 73 MB for SF (1,302,039 rows), so it is streamed with a plain
    # reader rather than loaded. Rows are grouped by trip in 511's file, but GTFS does
    # not promise that, so each trip's stops are collected and sorted here.
    known_stops = stops.root
    sequences: dict[str, list[tuple[int, str]]] = defaultdict(list)
    with zf.open("stop_times.txt") as raw:
        reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
        header = next(reader)
        trip_col, stop_col, seq_col = (header.index(c) for c in ("trip_id", "stop_id", "stop_sequence"))
        interned: dict[str, str] = {}
        for row in reader:
            trip_id, stop_id = row[trip_col], row[stop_col]
            if trip_id not in trips:
                raise GtfsError(f"stop_times.txt names trip {trip_id}, which trips.txt does not list")
            platform = interned.get(stop_id)
            if platform is None:
                platform = ref(operator, stop_id)
                if platform not in known_stops:
                    raise GtfsError(f"trip {trip_id} stops at {stop_id}, which is not a stop in stops.txt")
                interned[stop_id] = platform
            # stop_sequence is only required to increase, not to be contiguous, and
            # compares as a number: 10 comes after 9.
            sequences[trip_id].append((int(row[seq_col]), platform))

    groups: dict[tuple[str, int, tuple[str, ...]], Counter[str]] = defaultdict(Counter)
    for trip_id, (route_id, direction, headsign) in trips.items():
        seq = sequences.get(trip_id)
        if not seq:
            raise GtfsError(f"trip {trip_id} has no stop times")
        seq.sort()
        if any(a[0] == b[0] for a, b in zip(seq, seq[1:])):
            raise GtfsError(f"trip {trip_id} repeats a stop_sequence")
        groups[(route_id, direction, tuple(p for _, p in seq))][headsign] += 1

    out: dict[str, list[Pattern]] = defaultdict(list)
    for (route_id, direction, seq), headsigns in groups.items():
        # Trips running one sequence can still carry different headsigns. The most
        # common one names the pattern; a tie goes to the lexicographically first so
        # that a rebuild of the same zip is byte-identical.
        headsign = min(headsigns.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        out[ref(operator, route_id)].append(
            Pattern(direction=direction, headsign=headsign, trips=headsigns.total(), stops=list(seq))
        )
    return SnapshotPatterns(dict(out))


# MARK: - Service period


def _date(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y%m%d").date()


def _service_period(zf: zipfile.ZipFile) -> tuple[date, date]:
    """feed_info's own dates, which is what 511 means by the feed's period (SF:
    20260829-20270115). Both are optional in GTFS, so fall back to the calendar."""
    if _has(zf, "feed_info.txt"):
        for row in _rows(zf, "feed_info.txt"):
            start, end = row.get("feed_start_date", "").strip(), row.get("feed_end_date", "").strip()
            if start and end:
                return _date(start), _date(end)

    days: list[date] = []
    if _has(zf, "calendar.txt"):
        for row in _rows(zf, "calendar.txt"):
            days += [_date(row["start_date"]), _date(row["end_date"])]
    # A feed may define service by calendar_dates alone. Only added days
    # (exception_type 1) extend the period; a removal cannot.
    if _has(zf, "calendar_dates.txt"):
        days += [_date(row["date"]) for row in _rows(zf, "calendar_dates.txt") if row.get("exception_type") == "1"]
    if not days:
        raise GtfsError("no service period: feed_info.txt, calendar.txt and calendar_dates.txt give no dates")
    return min(days), max(days)
