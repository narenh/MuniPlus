"""Response shapes built from the loaded network, plus ``Health`` and ``Problem``.

The station and line shapes are what the retired v1 API served the app. The
editor serves only ``ShapesResponse`` from here today; ``Network`` still builds
the rest because it derives them from the curation in one pass, and they are the
natural starting point for an exporter to the app's ``data.json``. Editorial
fields (``note``, ``verified``) are not part of them.

``Alert`` stays only because ``StationDetail`` names it; nothing fills it now.
"""

from typing import Literal

from .base import Wire
from .ids import Color, Heading, LineId, Mode, Operator, StopId, ShapeId, StationId, SubwayId, TransferMode

# MARK: - Stations


class PlatformSummary(Wire):
    """One place a rider stands. Usually one 511 stop; where 511 numbers one place
    more than once, all of them, and everything about the platform (its lines,
    its arrivals) is the union of its stops'."""

    id: StopId
    """The platform's primary stop."""
    heading: Heading
    lines: list[LineId]
    """Every line that stops at any of its stops. Derived from 511's stop patterns."""
    stops: list[StopId]
    """Its stops in 511's data, the primary first. Arrivals asked for by any of
    them are the whole platform's."""
    former_ids: list[StopId]
    """Ids this platform has had and lost (a stop 511 retired or renumbered). A
    stored platform id is found by ``id``, then ``stops``, then ``formerIds``;
    arrivals asked for by a former id are answered under the current ``id``."""


class TransferOut(Wire):
    to: StationId
    name: str
    mode: TransferMode


class StationSummary(Wire):
    """One entry in ``GET /api/stations``: enough to draw the map, search, and ask
    for arrivals without a second request."""

    id: StationId
    name: str
    lat: float
    lon: float
    """The centroid of the station's live platforms. Never null."""
    lines: list[LineId]
    modes: list[Mode]
    operators: list[Operator]
    """Every operator serving the station, as 511 codes (``SF``, ``BA``): those
    with platforms here, then those with none in the data yet (BART at
    Embarcadero). Stable in meaning as other operators' platforms are added."""
    platforms: list[PlatformSummary]
    transfers: list[TransferOut]
    """Stations reachable on foot, sorted by the other station's name. Here and not
    only on the detail, so a station list can show every row's transfers without
    a request per station."""


class SubwayOut(Wire):
    id: SubwayId
    name: str
    stations: list[StationId]
    """In order along the subway. Only stations this API serves."""


class StationsResponse(Wire):
    version: str
    """The data repo commit this data was built from. Also sent as the ETag."""
    former_ids: dict[StationId, StationId]
    """Old id -> current id, for migrating favourites (``mongomery`` -> ``montgomery``)."""
    stations: list[StationSummary]
    subways: list[SubwayOut]


class PlatformDetail(PlatformSummary):
    name: str | None
    """Signage ("Platform 1"), where curated."""
    stop_name: str
    """511's name for the primary stop."""
    lat: float
    lon: float
    """The centroid of its stops, which are metres apart where there are several."""


class SubwayRef(Wire):
    id: SubwayId
    name: str


class StationDetail(StationSummary):
    platforms: list[PlatformDetail]
    subways: list[SubwayRef]
    alerts: list["Alert"]
    """Alerts active now that touch this station's platforms."""


class StationDetailResponse(Wire):
    version: str
    station: StationDetail


# MARK: - Lines


class LineSummary(Wire):
    id: LineId
    short_name: str
    name: str
    color: Color | None
    text_color: Color | None
    mode: Mode
    hidden: bool
    replaces: list[LineId]
    owl: bool
    """Overnight service: 511 names the line an Owl (``LOWL Owl Taraval``, ``90 San
    Bruno Owl``). With ``replaces``, it tells the owl that covers a line's corridor
    at night from a bus standing in for the line by day (``KBUS``)."""


class LinesResponse(Wire):
    version: str
    lines: list[LineSummary]


class Direction(Wire):
    direction: Literal[0, 1]
    headsign: str
    stations: list[StationId]
    """The most-run pattern in this direction, mapped to stations, in order. Its
    first and last entries are the line's terminals in this direction."""
    stops: list[StopId]
    """The same pattern as stops. Stops no station claims are left out."""
    shape: ShapeId | None = None
    """The path this pattern drives, a key into ``GET /api/shapes``. None when the
    snapshot has no shape for it; draw through ``stops`` instead."""


class LineDetail(LineSummary):
    directions: list[Direction]


class LineDetailResponse(Wire):
    version: str
    line: LineDetail


class ShapesResponse(Wire):
    shapes: dict[ShapeId, list[tuple[float, float]]]
    """Every shape a line direction names, as ``[lon, lat]`` points: a GeoJSON
    LineString's ``coordinates``, ready to draw. Simplified to within half a metre
    of the feed's path (``transit_editor.data.shapes``)."""


# MARK: - Alerts


class ActivePeriod(Wire):
    start: int | None
    end: int | None


class Alert(Wire):
    id: str
    header: str
    description: str
    active_periods: list[ActivePeriod]
    lines: list[LineId]
    stops: list[StopId]
    """As 511 names them."""
    stations: list[StationId]
    """Resolved from ``stops`` through curation."""
    url: str | None


StationDetail.model_rebuild()


# MARK: - Health


class Health(Wire):
    ok: bool
    """The data checkout is loaded. The HTTP status is 200 either way: this is
    for a person reading it, not for a container restart policy."""
    version: str | None
    problems: list[str]
    """Why ``ok`` is false, in words: a missing or unreadable checkout."""


class Problem(Wire):
    """Every error response."""

    error: str
    message: str
