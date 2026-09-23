"""Public response shapes for ``/api/*``.

These are the contract with the app. They are built from the loaded network and
the realtime state and never expose the curation files directly, so the files can
be reorganised without breaking a client.

Times in realtime responses are Unix epoch seconds, because the app counts down
against its own clock. ``fetchedAt`` says how old the underlying 511 data is.
Editorial fields (``note``, ``verified``) are not part of the public API.
"""

from typing import Literal

from .base import Wire
from .ids import Color, Heading, LineId, Mode, Operator, PlatformId, ShapeId, StationId, SubwayId, TransferMode

# MARK: - Stations


class PlatformSummary(Wire):
    id: PlatformId
    heading: Heading
    lines: list[LineId]
    """Derived from 511's stop patterns."""


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
    platforms: list[PlatformSummary]


class StationsResponse(Wire):
    version: str
    """sf-transit commit this data was built from. Also sent as the ETag."""
    former_ids: dict[StationId, StationId]
    """Old id -> current id, for migrating favourites (``mongomery`` -> ``montgomery``)."""
    stations: list[StationSummary]


class PlatformDetail(PlatformSummary):
    name: str | None
    """Signage ("Platform 1"), where curated."""
    stop_name: str
    """511's name for the stop."""
    lat: float
    lon: float


class TransferOut(Wire):
    to: StationId
    name: str
    mode: TransferMode


class SubwayRef(Wire):
    id: SubwayId
    name: str


class StationDetail(StationSummary):
    platforms: list[PlatformDetail]
    transfers: list[TransferOut]
    transfer_agencies: list[Operator]
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


class LinesResponse(Wire):
    version: str
    lines: list[LineSummary]


class Direction(Wire):
    direction: Literal[0, 1]
    headsign: str
    stations: list[StationId]
    """The most-run pattern in this direction, mapped to stations, in order. Its
    first and last entries are the line's terminals in this direction."""
    platforms: list[PlatformId]
    """The same pattern as platforms. Stops no station claims are left out."""
    shape: ShapeId | None = None
    """The path this pattern drives, a key into ``GET /api/shapes``. None when the
    snapshot has no shape for it; draw through ``platforms`` instead."""


class LineDetail(LineSummary):
    directions: list[Direction]


class LineDetailResponse(Wire):
    version: str
    line: LineDetail


class ShapesResponse(Wire):
    shapes: dict[ShapeId, list[tuple[float, float]]]
    """Every shape a line direction names, as ``[lon, lat]`` points: a GeoJSON
    LineString's ``coordinates``, ready to draw. Simplified to within half a metre
    of the feed's path (``app.data.shapes``)."""


# MARK: - Realtime


class Arrival(Wire):
    line: LineId
    direction: Literal[0, 1]
    headsign: str
    """From the line and direction. Per-trip headsigns need realtime trip ids to
    join to GTFS trips, which is not yet verified."""
    time: int
    kind: Literal["arrival", "departure"]
    """``departure`` where the feed gives only a departure time, which it does at a
    trip's first stop: the vehicle starts its run here."""
    terminates: bool
    """True where this is the trip's last stop. Per trip, so a short turn is caught."""
    trip: str
    vehicle: str | None


class ArrivalsResponse(Wire):
    fetched_at: int | None
    """When the TripUpdates feed behind this answer came back from 511. Null before
    the first successful fetch."""
    platforms: dict[PlatformId, list[Arrival]]
    """Every requested platform is present; one with nothing coming is an empty list."""


VehicleStatus = Literal["incomingAt", "stoppedAt", "inTransitTo"]


class Vehicle(Wire):
    id: str
    line: LineId
    direction: Literal[0, 1] | None
    trip: str
    lat: float
    lon: float
    bearing: float | None
    """Degrees clockwise from north. Null where 511 sends 0, which is how it sends
    "unknown": a vehicle genuinely heading due north is indistinguishable from one
    with no bearing, and the latter is far more common."""
    speed: float | None
    """Metres per second. Null where 511 sends 0 for the same reason."""
    stop: PlatformId | None
    status: VehicleStatus | None
    reported_at: int


class VehiclesResponse(Wire):
    fetched_at: int | None
    vehicles: list[Vehicle]
    """In-service vehicles only: those on a trip with a line."""


class ActivePeriod(Wire):
    start: int | None
    end: int | None


class Alert(Wire):
    id: str
    header: str
    description: str
    active_periods: list[ActivePeriod]
    lines: list[LineId]
    platforms: list[PlatformId]
    stations: list[StationId]
    """Resolved from ``platforms`` through curation."""
    url: str | None


class AlertsResponse(Wire):
    fetched_at: int | None
    alerts: list[Alert]
    """Alerts active now."""


StationDetail.model_rebuild()


# MARK: - Health


class FeedHealth(Wire):
    interval_seconds: int
    fetched_at: int | None
    age_seconds: int | None
    last_error: str | None


class BudgetHealth(Wire):
    per_hour: int
    used_last_hour: int
    last_rate_limit_remaining: int | None
    """511's own ``RateLimit-Remaining`` header from the most recent call."""


class Health(Wire):
    ok: bool
    """The data is loaded and, where polling is on, every feed is fresh (fetched
    within three of its intervals). The HTTP status is 200 either way: this is
    for a person reading it, not for a container restart policy."""
    version: str | None
    fixtures: bool
    feeds: dict[str, FeedHealth]
    """Keyed ``<operator>:<feed>``, e.g. ``SF:tripupdates``."""
    budget: BudgetHealth
    problems: list[str]
    """Why ``ok`` is false, in words: a missing checkout, a stale feed."""


class Problem(Wire):
    """Every error response."""

    error: str
    message: str
