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
from ..names import tidy_stop_name
from collections import Counter, defaultdict
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.models.ids import ref, upstream_of
from app.models.snapshot import (
    Pattern,
    Snapshot,
    SnapshotLine,
    SnapshotLines,
    SnapshotMeta,
    SnapshotPatterns,
    SnapshotShapes,
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
        service_from, service_to = _service_period(zf)
        days = service_days(zf, service_from, service_to)
        patterns = _patterns(zf, routes, stops, days, operator)
        shapes = _shapes(zf, patterns, operator)

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
    return Snapshot(meta=meta, stops=stops, lines=lines, patterns=patterns, shapes=shapes)


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
            name=tidy_stop_name(row["stop_name"]),
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
    days: dict[str, int],
    operator: str,
) -> SnapshotPatterns:
    """``trips`` on a pattern counts trips run in the service period: each trip row
    once per date its service_id runs. A plain row count weighs a service that runs
    13 days the same as one that runs 82, and 511's SF feed has exactly that
    (weekday services 79714 and 78968), which picks a different most-run pattern
    for the TBUS."""
    trips: dict[str, tuple[str, int, str, int, str]] = {}
    for row in _rows(zf, "trips.txt"):
        route_id = row["route_id"]
        if route_id not in routes:
            raise GtfsError(f"trip {row['trip_id']} is on route {route_id}, which routes.txt does not list")
        service_id = row["service_id"]
        if service_id not in days:
            raise GtfsError(f"trip {row['trip_id']} has service_id {service_id}, which no calendar file defines")
        # The realtime feeds key directions on this same 0/1, so a trip without one
        # could never be matched to a pattern. 511's SF trips all have it (34,668).
        direction = row.get("direction_id", "")
        if direction not in ("0", "1"):
            raise GtfsError(f"trip {row['trip_id']} has direction_id {direction!r}")
        # shape_id is optional in GTFS. Blank means this trip has no drawn path, and
        # its pattern falls back to a line through the stops.
        shape = row.get("shape_id", "").strip()
        trips[row["trip_id"]] = (route_id, int(direction), row.get("trip_headsign", ""), days[service_id], shape)

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
    shapes: dict[tuple[str, int, tuple[str, ...]], Counter[str]] = defaultdict(Counter)
    for trip_id, (route_id, direction, headsign, runs, shape) in trips.items():
        seq = sequences.get(trip_id)
        if not seq:
            raise GtfsError(f"trip {trip_id} has no stop times")
        seq.sort()
        if any(a[0] == b[0] for a, b in zip(seq, seq[1:])):
            raise GtfsError(f"trip {trip_id} repeats a stop_sequence")
        # A trip whose service runs on no date in the period is not part of it. It
        # is still validated above: a malformed trip is a feed problem either way.
        if runs:
            key = (route_id, direction, tuple(p for _, p in seq))
            groups[key][headsign] += runs
            if shape:
                shapes[key][shape] += runs

    out: dict[str, list[Pattern]] = defaultdict(list)
    for (route_id, direction, seq), headsigns in groups.items():
        # Trips running one sequence can still carry different headsigns. The most
        # common one, weighted by days run like ``trips``, names the pattern; a tie
        # goes to the lexicographically first so a rebuild of the same zip is
        # byte-identical.
        headsign = min(headsigns.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        # Likewise the shape: trips stopping at the same stops can still drive
        # different paths (a detour that skips no stop), and the one most run is
        # the one to draw.
        drawn = shapes.get((route_id, direction, seq))
        shape = ref(operator, min(drawn.items(), key=lambda kv: (-kv[1], kv[0]))[0]) if drawn else None
        out[ref(operator, route_id)].append(
            Pattern(direction=direction, headsign=headsign, trips=headsigns.total(), stops=list(seq), shape=shape)
        )
    return SnapshotPatterns(dict(out))


# MARK: - Shapes


def _shapes(zf: zipfile.ZipFile, patterns: SnapshotPatterns, operator: str) -> SnapshotShapes:
    """The path of every shape a pattern names, in ``shape_pt_sequence`` order.

    A pattern naming a shape ``shapes.txt`` does not have raises, as a trip at an
    unknown stop does: the feed contradicts itself. A feed with no shapes at all is
    allowed (GTFS makes them optional) and gives patterns with none.
    """
    wanted = {upstream_of(p.shape) for line in patterns.root.values() for p in line if p.shape}
    if not wanted:
        return SnapshotShapes()
    if not _has(zf, "shapes.txt"):
        raise GtfsError("trips.txt names shapes, but the feed has no shapes.txt")

    points: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
    for row in _rows(zf, "shapes.txt"):
        shape_id = row["shape_id"]
        if shape_id in wanted:
            # Like stop_sequence, shape_pt_sequence need only increase, and SFMTA's
            # own feed skips numbers (1, 3, ...), so it is sorted, never indexed.
            seq = int(row["shape_pt_sequence"])
            points[shape_id].append((seq, float(row["shape_pt_lon"]), float(row["shape_pt_lat"])))

    out = {}
    for shape_id in wanted:
        pts = points.get(shape_id)
        if not pts:
            raise GtfsError(f"trips.txt names shape {shape_id}, which shapes.txt does not list")
        pts.sort()
        if any(a[0] == b[0] for a, b in zip(pts, pts[1:])):
            raise GtfsError(f"shape {shape_id} repeats a shape_pt_sequence")
        out[ref(operator, shape_id)] = [(lon, lat) for _, lon, lat in pts]
    return SnapshotShapes(out)


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


def service_days(zf: zipfile.ZipFile, start: date, end: date) -> dict[str, int]:
    """service_id -> how many dates in ``start..end`` (inclusive) it runs.

    calendar.txt gives weekday flags over a date range; calendar_dates.txt then adds
    (exception_type 1) or removes (2) single dates. Both are needed: 511's SF feed
    defines Saturday and Sunday service in calendar.txt but weekday service only
    through calendar_dates.txt. A service_id that appears in either file but never
    runs in the period maps to 0.
    """
    names = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    weekly: dict[str, tuple[date, date, tuple[bool, ...]]] = {}
    if _has(zf, "calendar.txt"):
        for row in _rows(zf, "calendar.txt"):
            flags = tuple(row[n].strip() == "1" for n in names)
            weekly[row["service_id"]] = (_date(row["start_date"]), _date(row["end_date"]), flags)
    exceptions: dict[date, dict[str, str]] = defaultdict(dict)
    if _has(zf, "calendar_dates.txt"):
        for row in _rows(zf, "calendar_dates.txt"):
            kind = row["exception_type"].strip()
            if kind not in ("1", "2"):
                raise GtfsError(f"calendar_dates.txt: exception_type {kind!r} for {row['service_id']} on {row['date']}")
            exceptions[_date(row["date"])][row["service_id"]] = kind

    counts = dict.fromkeys(weekly, 0) | {sid: 0 for on_day in exceptions.values() for sid in on_day}
    day = start
    while day <= end:
        running = {sid for sid, (lo, hi, flags) in weekly.items() if lo <= day <= hi and flags[day.weekday()]}
        for sid, kind in exceptions.get(day, {}).items():
            if kind == "1":
                running.add(sid)
            else:
                running.discard(sid)
        for sid in running:
            counts[sid] += 1
        day += timedelta(days=1)
    return counts
