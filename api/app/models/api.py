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
from .ids import Color, Heading, LineId, Mode, Operator, StopId, ShapeId, StationId, SubwayId, TransferMode, TripId, VehicleId

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
    """sf-transit commit this data was built from. Also sent as the ETag."""
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
    trip: TripId
    vehicle: VehicleId | None


class ArrivalsResponse(Wire):
    fetched_at: int | None
    """When the TripUpdates feed behind this answer came back from 511. Null before
    the first successful fetch."""
    feed_at: int | None
    """511's own time for the feed behind this answer (its header timestamp): how
    old the data is, as opposed to ``fetchedAt``, when this server downloaded it.
    With several operators, the oldest. Null before the first successful fetch."""
    refresh_after: int
    """Seconds until the server can have newer data (its next fetch from 511, plus
    a margin). Poll no sooner: the answer cannot change before then."""
    platforms: dict[StopId, list[Arrival]]
    """Keyed by platform id. Ask with platform ids (the ``id`` of each platform in
    ``/stations``). Another stop of a platform is answered under the platform's
    id, the whole platform's arrivals merged across its stops, so two stops of one
    platform are one entry; a stop no platform claims is answered under itself.
    Every one is present, as an empty list when nothing is coming."""


VehicleStatus = Literal["incomingAt", "stoppedAt", "inTransitTo"]


class Vehicle(Wire):
    id: VehicleId
    line: LineId
    direction: Literal[0, 1] | None
    trip: TripId
    lat: float
    lon: float
    bearing: float | None
    """Degrees clockwise from north. Null where 511 sends 0, which is how it sends
    "unknown": a vehicle genuinely heading due north is indistinguishable from one
    with no bearing, and the latter is far more common."""
    speed: float | None
    """Metres per second. Null where 511 sends 0 for the same reason."""
    stop: StopId | None
    status: VehicleStatus | None


class VehiclesResponse(Wire):
    fetched_at: int | None
    feed_at: int | None
    """511's own time for the feed behind this answer (its header timestamp): how
    old the data is, as opposed to ``fetchedAt``, when this server downloaded it.
    With several operators, the oldest. Null before the first successful fetch."""
    refresh_after: int
    """Seconds until the server can have newer positions. See ``ArrivalsResponse``."""
    vehicles: list[Vehicle]
    """In-service vehicles only: those on a trip with a line. There is no
    per-vehicle report time: 511 stamps a whole feed with one, so ``feedAt`` is
    the age of every position here."""


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


class AlertsResponse(Wire):
    fetched_at: int | None
    feed_at: int | None
    """511's own time for the feed behind this answer (its header timestamp): how
    old the data is, as opposed to ``fetchedAt``, when this server downloaded it.
    With several operators, the oldest. Null before the first successful fetch."""
    refresh_after: int
    """Seconds until the server can have newer alerts. See ``ArrivalsResponse``."""
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
