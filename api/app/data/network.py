"""The loaded network: curation x snapshots, with every derived value computed once.

A ``Network`` is built from one ``Curation``, the snapshots for each operator and
the sf-transit commit they came from, and never changes after that. When the data
changes (an editor save, a pull) the app builds a new one and swaps it in whole,
so a request never sees half of one version and half of another. Everything the
endpoints serve is computed here up front; the models it hands out are shared
between requests and must be treated as read-only.

The rules for what is derived are in PLAN.md ("Derived at load time"). The
network is also built from curation the editor has not saved yet, which may fail
validation, so nothing here assumes the curation is valid. Where invalid data
forces a choice, the first claim wins, taking stations in id order: a platform
listed by two stations belongs to the one whose id sorts first, and is left out of
the other. The validator reports the conflict; this module only has to stay
consistent while it exists.
"""

import hashlib
import json
import re
from collections.abc import Iterable, Mapping

from ..names import ACRONYMS, FIXUPS, MINOR_WORDS
from ..models.api import (
    Direction,
    LineDetail,
    LinesResponse,
    LineSummary,
    PlatformDetail,
    PlatformSummary,
    ShapesResponse,
    StationDetail,
    StationsResponse,
    StationSummary,
    SubwayRef,
    TransferOut,
)
from ..models.curation import Curation, Station
from ..models.editor import Derived, DerivedPlatform, DerivedStation
from ..models.snapshot import Pattern, Point, Snapshot, SnapshotLine, SnapshotStop
from .shapes import simplify

# MARK: - Line names

# Ported from tools/build_bus_data.py (``titled``, ``phrase``, ``title_case``), the
# script that built the line names the App Store app ships in appdata/bus.json.
# SFMTA's long names are upper case like 511's, and that script's output is the
# naming people already see ("44 O'Shaughnessy", "91 3rd-19th Ave Owl", "714 BART
# Early Bird"), so the new server keeps it rather than use ``str.title()``, which
# gives "O'shaughnessy", "3Rd-19Th" and "Bart".
#
# Its ``pretty`` is not ported: it drops street suffixes and splits on "&" to name
# a street corner from a stop name ("16th St & Rhode Island St" -> "16th & Rhode
# Island"), which is right for a stop and wrong for a line ("Market & Wharves",
# "Ashbury-18th St"). Station names are curated, so nothing here names a corner.
ORDINAL = re.compile(r"\d+(st|nd|rd|th)")


def _titled(word: str) -> str:
    """One word, keeping ordinals, acronyms and O'Names intact. ``word`` is lower case."""
    for sep in ("-", "/"):
        if sep in word:
            return sep.join(_titled(w) for w in word.split(sep))
    # One addition to the original: leading punctuation ("(owl") is set aside so the
    # letter after it is the one capitalised. The original gave "(owl".
    lead = re.match(r"\W*", word).group()
    word = word[len(lead):]
    if not word or ORDINAL.fullmatch(word):
        return lead + word
    if word in ACRONYMS:
        return lead + word.upper()
    if m := re.fullmatch(r"([a-z])'([a-z].*)", word):  # o'shaughnessy, not joseph's
        word = f"{m.group(1).upper()}'{m.group(2)[:1].upper()}{m.group(2)[1:]}"
    else:
        word = word[:1].upper() + word[1:]
    return lead + FIXUPS.get(word, word)


def title_case(text: str) -> str:
    """``"POWELL-HYDE CABLE CAR"`` -> ``"Powell-Hyde Cable Car"``."""
    words = text.lower().split()
    return " ".join(w if i and w in MINOR_WORDS else _titled(w) for i, w in enumerate(words))


def default_line_name(line: SnapshotLine) -> str:
    return f"{line.short_name} {title_case(line.long_name)}".strip()


# MARK: - Ordering

MODE_ORDER = ("metro", "streetcar", "cableway", "bus")
"""Rail before road, the order the app's line rail and legend use: the subway lines
people navigate by, then the F, the cable cars, then buses. A mode 511 adds later
(another operator's ``rail``, ``ferry``) sorts after these, alphabetically."""


def _mode_key(mode: str) -> tuple[int, str]:
    return (MODE_ORDER.index(mode), "") if mode in MODE_ORDER else (len(MODE_ORDER), mode)


def _natural_key(short_name: str) -> tuple:
    # Numbers compare as numbers, so 5 < 5R < 38 < 38R, and every numbered line
    # comes before the lettered ones (LOWL, NOWL) within a mode.
    return tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in re.findall(r"\d+|\D+", short_name))


def _line_key(line: LineSummary) -> tuple:
    return (_mode_key(line.mode), _natural_key(line.short_name), line.id)


# MARK: - Network


def _centroid(points: Iterable[tuple[float, float]]) -> tuple[float, float] | None:
    points = list(points)
    if not points:
        return None
    # Six places is ~10 cm, and the precision 511 publishes; without rounding the
    # mean of two coordinates prints as 37.79292200000001.
    lat = round(sum(p[0] for p in points) / len(points), 6)
    lon = round(sum(p[1] for p in points) / len(points), 6)
    return lat, lon


def _most_run(patterns: Iterable[Pattern]) -> dict[int, Pattern]:
    """The most-run pattern per direction. Ties go to the first in the file's own
    order (most trips first, then by stop sequence), so a reload never flips them."""
    best: dict[int, Pattern] = {}
    for pattern in patterns:
        if pattern.direction not in best or pattern.trips > best[pattern.direction].trips:
            best[pattern.direction] = pattern
    return best


class Network:
    """Everything the public API and the editor need from one version of the data."""

    __slots__ = (
        "_version",
        "_station_of",
        "_stops",
        "_headsigns",
        "_derived",
        "_stations",
        "_station_details",
        "_former_ids",
        "_lines",
        "_line_details",
        "_shape_points",
        "_shapes",
    )

    def __init__(self, curation: Curation, snapshots: Mapping[str, Snapshot], version: str):
        self._version = version
        stations = dict(sorted(curation.stations.stations.items()))
        overrides = curation.lines.root

        stops: dict[str, SnapshotStop] = {}
        snapshot_lines: dict[str, SnapshotLine] = {}
        patterns: dict[str, list[Pattern]] = {}
        shapes: dict[str, list[Point]] = {}
        for _, snapshot in sorted(snapshots.items()):
            stops.update(snapshot.stops.root)
            snapshot_lines.update(snapshot.lines.root)
            patterns.update(snapshot.patterns.root)
            shapes.update(snapshot.shapes.root)
        self._stops = stops

        # Lines. A line exists if 511 lists it; an override for any other line is a
        # warning in the validator and has nothing to attach to here.
        lines: dict[str, LineSummary] = {}
        for line_id, snap in snapshot_lines.items():
            o = overrides.get(line_id)
            lines[line_id] = LineSummary(
                id=line_id,
                short_name=snap.short_name,
                name=(o and o.name) or default_line_name(snap),
                color=(o and o.color) or snap.color,
                text_color=(o and o.text_color) or snap.text_color,
                mode=(o and o.mode) or snap.mode,
                hidden=bool(o and o.hidden),
                replaces=list(o.replaces) if o else [],
            )
        ordered = sorted(lines.values(), key=_line_key)
        rank = {line.id: i for i, line in enumerate(ordered)}

        def in_line_order(ids: Iterable[str]) -> list[str]:
            return sorted(set(ids), key=rank.__getitem__)

        # Platform lines: every pattern that stops there, not only the most-run one,
        # so a short turn or school trip still lists its line at the stops it serves.
        served: dict[str, set[str]] = {}
        for line_id, line_patterns in patterns.items():
            if line_id not in lines:
                continue
            for pattern in line_patterns:
                for stop in pattern.stops:
                    served.setdefault(stop, set()).add(line_id)
        platform_lines = {pid: in_line_order(ids) for pid, ids in served.items()}

        # Ownership: the first claim wins (see the module docstring).
        station_of: dict[str, str] = {}
        owned: dict[str, list] = {}
        for sid, station in stations.items():
            owned[sid] = []
            for platform in station.platforms:
                if platform.id not in station_of:
                    station_of[platform.id] = sid
                    owned[sid].append(platform)
        self._station_of = station_of

        # Derived platforms: every curated platform, and every stop in a snapshot, so
        # the editor's review queue can show what serves a stop nobody has assigned.
        derived_platforms: dict[str, DerivedPlatform] = {}
        for pid in sorted(station_of.keys() | stops.keys()):
            stop = stops.get(pid)
            derived_platforms[pid] = DerivedPlatform(
                live=stop is not None,
                lines=platform_lines.get(pid, []),
                lat=stop.lat if stop else None,
                lon=stop.lon if stop else None,
                stop_name=stop.name if stop else None,
            )

        # Stations.
        derived_stations: dict[str, DerivedStation] = {}
        summaries: dict[str, StationSummary] = {}
        details: dict[str, StationDetail] = {}
        subways_of: dict[str, list[SubwayRef]] = {}
        for subway_id, subway in sorted(curation.stations.subways.items()):
            for sid in dict.fromkeys(subway.stations):
                subways_of.setdefault(sid, []).append(SubwayRef(id=subway_id, name=subway.name))

        for sid, station in stations.items():
            live = [p for p in owned[sid] if p.id in stops]
            centre = _centroid((stops[p.id].lat, stops[p.id].lon) for p in live)
            station_lines = in_line_order(line for p in live for line in platform_lines.get(p.id, []))
            modes = sorted({lines[line].mode for line in station_lines}, key=_mode_key)
            derived_stations[sid] = DerivedStation(
                lat=centre[0] if centre else None,
                lon=centre[1] if centre else None,
                lines=station_lines,
                modes=modes,
            )
            if centre is None:
                # No coordinate to draw it at. Kept in ``derived`` (the editor must see
                # it to fix it) but left out of the public API, which promises one.
                continue
            summaries[sid] = StationSummary(
                id=sid,
                name=station.name,
                lat=centre[0],
                lon=centre[1],
                lines=station_lines,
                modes=modes,
                platforms=[
                    PlatformSummary(id=p.id, heading=p.heading, lines=platform_lines.get(p.id, []))
                    for p in live
                ],
            )

        for sid, summary in summaries.items():
            station = stations[sid]
            details[sid] = StationDetail(
                **{k: v for k, v in summary if k != "platforms"},
                platforms=[
                    PlatformDetail(
                        id=p.id,
                        heading=p.heading,
                        lines=platform_lines.get(p.id, []),
                        name=p.name,
                        stop_name=stops[p.id].name,
                        lat=stops[p.id].lat,
                        lon=stops[p.id].lon,
                    )
                    for p in owned[sid]
                    if p.id in stops
                ],
                # A transfer to a station the API does not serve (unknown, or with no
                # live platforms) would be a link to a 404, so it is left out.
                transfers=[
                    TransferOut(to=t.to, name=stations[t.to].name, mode=t.mode)
                    for t in station.transfers
                    if t.to in summaries
                ],
                transfer_agencies=list(station.transfer_agencies),
                subways=subways_of.get(sid, []),
                alerts=[],
            )

        self._stations = StationsResponse(
            version=version,
            former_ids=_former_ids(stations, summaries),
            stations=list(summaries.values()),
        )
        self._former_ids = self._stations.former_ids
        self._station_details = details

        # Line diagrams.
        self._headsigns: dict[tuple[str, int], str] = {}
        drawn: dict[str, list[Point]] = {}
        self._line_details: dict[str, LineDetail] = {}
        for line in ordered:
            directions = []
            for direction, pattern in sorted(_most_run(patterns.get(line.id, [])).items()):
                self._headsigns[(line.id, direction)] = pattern.headsign
                claimed = [s for s in pattern.stops if s in station_of and s in stops]
                along: list[str] = []
                for pid in claimed:
                    # A station with two poles on one block appears once, not twice.
                    if not along or along[-1] != station_of[pid]:
                        along.append(station_of[pid])
                # A shape the snapshot names but does not carry is left out rather than
                # sent as a key the shapes endpoint cannot answer.
                shape = pattern.shape if pattern.shape in shapes else None
                if shape:
                    drawn[shape] = shapes[shape]
                directions.append(
                    Direction(
                        direction=direction, headsign=pattern.headsign, stations=along, platforms=claimed, shape=shape
                    )
                )
            self._line_details[line.id] = LineDetail(**dict(line), directions=directions)

        self._lines = LinesResponse(version=version, lines=ordered)
        # Simplified on first request, not here: the editor builds a network for every
        # validate, and none of those is ever asked for its shapes.
        self._shape_points = dict(sorted(drawn.items()))
        self._shapes: tuple[ShapesResponse, str] | None = None
        self._derived = Derived(
            stations=derived_stations,
            platforms=derived_platforms,
            lines={line.id: self._line_details[line.id] for line in ordered},
        )

    # MARK: Interface for realtime (PLAN.md, "Interfaces between tracks")

    @property
    def version(self) -> str:
        """The sf-transit commit this network was built from."""
        return self._version

    def station_of(self, platform_id: str) -> str | None:
        """The station a platform is assigned to, live or not."""
        return self._station_of.get(platform_id)

    def headsign(self, line_id: str, direction: int) -> str | None:
        """The most-run pattern's headsign for this line and direction."""
        return self._headsigns.get((line_id, direction))

    def is_live(self, platform_id: str) -> bool:
        """Assigned to a station and in the snapshot."""
        return platform_id in self._station_of and platform_id in self._stops

    # MARK: Endpoints

    def stations(self) -> StationsResponse:
        return self._stations

    def station(self, station_id: str) -> StationDetail | None:
        """Detail with ``alerts`` empty; the endpoint fills them per request."""
        return self._station_details.get(station_id)

    def current_id(self, former_id: str) -> str | None:
        """Where a former station id now lives, if it redirects anywhere."""
        return self._former_ids.get(former_id)

    def lines(self) -> LinesResponse:
        return self._lines

    def line(self, line_id: str) -> LineDetail | None:
        return self._line_details.get(line_id)

    def shapes(self) -> tuple[ShapesResponse, str]:
        """Every shape a line direction names, and a hash of them for an ETag.

        The hash is of the shapes alone, not the sf-transit version: a curation edit
        moves the version but not one point, and the shapes are the one response
        big enough that a client should only fetch it again when they change."""
        if self._shapes is None:
            response = ShapesResponse(shapes={sid: simplify(pts) for sid, pts in self._shape_points.items()})
            body = json.dumps(response.model_dump(mode="json"), separators=(",", ":")).encode()
            # Two requests racing here both compute the same value; either may win.
            self._shapes = (response, hashlib.sha256(body).hexdigest()[:16])
        return self._shapes

    # MARK: Editor

    def derived(self) -> Derived:
        return self._derived


def _former_ids(stations: Mapping[str, Station], public: Mapping[str, object]) -> dict[str, str]:
    """Old id -> current id, for redirects and the app's favourites migration.

    A live id always wins over a former one, and a former id claimed twice goes to
    the first station in id order, so a collision the validator reports can never
    redirect a working id somewhere else. Only stations the API serves are
    targets: a redirect to a 404 would help nobody.
    """
    out: dict[str, str] = {}
    for sid, station in stations.items():
        if sid not in public:
            continue
        for former in station.former_ids:
            if former not in stations and former not in out:
                out[former] = sid
    return dict(sorted(out.items()))
