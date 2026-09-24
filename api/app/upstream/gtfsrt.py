"""511's three GTFS-Realtime feeds, decoded into plain types.

Nothing here knows about stations, headsigns or the clock: it turns bytes into
facts about one operator's feed, with every id already qualified
(``SF:16992``). Joining those facts to the network happens in
``app.realtime.state``, at query time, because the network is replaced whole
whenever the editor saves and a decoded feed must not go stale with it.

Facts below were measured on the recorded feeds in ``tests/fixtures/realtime``
(SF, 2026-09-22 ~14:50 Pacific) and are asserted in
``tests/realtime/test_gtfsrt.py``, so a change in 511's behaviour shows up as a
failing test rather than a quiet regression.
"""

import gzip
from dataclasses import dataclass
from typing import Literal, get_args

from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2 as pb

from app.models.api import VehicleStatus
from app.models.ids import ref

Feed = Literal["tripupdates", "vehiclepositions", "servicealerts"]
"""511's own path segment for each feed (``/transit/<feed>``)."""
FEEDS: tuple[Feed, ...] = get_args(Feed)

Kind = Literal["arrival", "departure"]


class FeedError(ValueError):
    """A payload that must not replace the last good copy."""


# MARK: - Types


@dataclass(frozen=True, slots=True)
class StopTime:
    stop: str
    time: int
    kind: Kind
    terminates: bool


@dataclass(frozen=True, slots=True)
class Trip:
    id: str
    line: str
    direction: Literal[0, 1]
    vehicle: str | None
    """From the TripUpdate's own vehicle descriptor. Present on all 1,435 trips in
    the fixture, including ones hours away, where it names the vehicle the block
    is assigned to (one vehicle appears on up to 7 trips)."""
    stops: tuple[StopTime, ...]


@dataclass(frozen=True, slots=True)
class TripUpdates:
    timestamp: int
    trips: tuple[Trip, ...]


@dataclass(frozen=True, slots=True)
class VehicleInfo:
    id: str
    line: str
    direction: Literal[0, 1] | None
    trip: str
    lat: float
    lon: float
    bearing: float | None
    speed: float | None
    stop: str | None
    status: VehicleStatus | None
    reported_at: int


@dataclass(frozen=True, slots=True)
class VehiclePositions:
    timestamp: int
    vehicles: tuple[VehicleInfo, ...]
    """In service only."""
    out_of_service: int
    """Left out for having no trip or no line (or an id that cannot be a ref, which
    none in the fixtures has). Kept as a count for tests and health."""


@dataclass(frozen=True, slots=True)
class AlertInfo:
    id: str
    header: str
    description: str
    periods: tuple[tuple[int | None, int | None], ...]
    lines: tuple[str, ...]
    stops: tuple[str, ...]
    url: str | None

    def active_at(self, now: int) -> bool:
        # GTFS-rt: no active_period means "active for as long as it is in the
        # feed". 511 writes ends as hh:59:59 (all 41 fixture alerts), which reads
        # as an inclusive bound, so ``now == end`` is still active.
        if not self.periods:
            return True
        return any((start is None or start <= now) and (end is None or now <= end) for start, end in self.periods)


@dataclass(frozen=True, slots=True)
class ServiceAlerts:
    timestamp: int
    alerts: tuple[AlertInfo, ...]


# MARK: - Decoding


def decode_trip_updates(payload: bytes, operator: str) -> TripUpdates:
    """Every stop's predicted time, per trip.

    A stop's time is ``arrival.time``, else ``departure.time`` with kind
    ``departure``. 511 never sends both on one stop (0 of 36,137 updates), so the
    two kinds do not compete. 1,032 updates are departure-only: 1,027 are a
    trip's first update, and the other 5 are the second update of route 9 trips
    whose first two stops (13245, 13243) are both departure-only. Reading
    arrivals alone would lose 15 stops entirely (13243, 14824, 15063, ...),
    because a trip's first stop is the only thing they ever are.

    ``terminates`` is set on the trip's last update when that update is an
    arrival. The kind check matters: 11 trips in the fixture consist of a single
    departure-only update (e.g. 12134401_M11, L at 17217, two hours out), and a
    trip that is only a departure is starting there, not ending.
    """
    feed = _parse(payload)
    trips: list[Trip] = []
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        descriptor = tu.trip
        if descriptor.schedule_relationship == pb.TripDescriptor.CANCELED:
            continue
        # Line, trip and direction are all required on an Arrival. All 1,435
        # fixture trips carry all three; one that does not cannot be labelled, so
        # it is dropped rather than guessed at.
        line = _qualify(operator, descriptor.route_id)
        trip_id = _qualify(operator, descriptor.trip_id)
        if line is None or trip_id is None or not descriptor.HasField("direction_id"):
            continue
        if descriptor.direction_id not in (0, 1):
            continue

        timed: list[tuple[str, int, Kind]] = []
        for update in tu.stop_time_update:
            if update.schedule_relationship == pb.TripUpdate.StopTimeUpdate.SKIPPED:
                continue
            stop = _qualify(operator, update.stop_id)
            if stop is None:
                continue
            if update.HasField("arrival") and update.arrival.time:
                timed.append((stop, update.arrival.time, "arrival"))
            elif update.HasField("departure") and update.departure.time:
                timed.append((stop, update.departure.time, "departure"))
        if not timed:
            continue

        last = len(timed) - 1
        stops = tuple(
            StopTime(stop, time, kind, terminates=(i == last and kind == "arrival"))
            for i, (stop, time, kind) in enumerate(timed)
        )
        vehicle = _qualify(operator, tu.vehicle.id) if tu.HasField("vehicle") else None
        trips.append(Trip(
            id=trip_id,
            line=line,
            direction=descriptor.direction_id,
            vehicle=vehicle,
            stops=stops,
        ))

    if not trips:
        raise FeedError("TripUpdates feed has no usable trips")
    return TripUpdates(timestamp=feed.header.timestamp, trips=tuple(trips))


_STATUS: dict[int, VehicleStatus] = {
    pb.VehiclePosition.INCOMING_AT: "incomingAt",
    pb.VehiclePosition.STOPPED_AT: "stoppedAt",
    pb.VehiclePosition.IN_TRANSIT_TO: "inTransitTo",
}


def decode_vehicle_positions(payload: bytes, operator: str) -> VehiclePositions:
    """In-service vehicles.

    Of 677 vehicles in vehiclepositions-1, 145 (21%) have no trip at all: parked,
    deadheading or signed off. None had a trip without a route_id, but either
    means the vehicle is not carrying anyone on a line, so both are left out.

    ``bearing`` and ``speed`` are always present on the wire and 511 fills them
    with 0 when unknown: 148 of 677 (22%) have bearing 0 and 406 (60%) speed 0.
    Even among the 532 in service, 58 report bearing 0, far more than would
    genuinely be heading due north, so 0 is read as "unknown" and becomes None.

    ``current_status`` is read with HasField because its proto default is
    IN_TRANSIT_TO: 2 in-service vehicles omit it, and reading the default would
    report them as moving.
    """
    feed = _parse(payload)
    vehicles: list[VehicleInfo] = []
    out_of_service = 0
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        vp = entity.vehicle
        line = _qualify(operator, vp.trip.route_id) if vp.HasField("trip") else None
        trip = _qualify(operator, vp.trip.trip_id) if vp.HasField("trip") else None
        raw_id = vp.vehicle.id if vp.HasField("vehicle") and vp.vehicle.id else entity.id
        vehicle_id = _qualify(operator, raw_id)
        if line is None or trip is None or vehicle_id is None or not vp.HasField("position"):
            out_of_service += 1
            continue
        direction = vp.trip.direction_id if vp.trip.HasField("direction_id") and vp.trip.direction_id in (0, 1) else None
        position = vp.position
        vehicles.append(VehicleInfo(
            id=vehicle_id,
            line=line,
            direction=direction,
            trip=trip,
            lat=position.latitude,
            lon=position.longitude,
            bearing=position.bearing if position.HasField("bearing") and position.bearing != 0 else None,
            speed=position.speed if position.HasField("speed") and position.speed != 0 else None,
            stop=_qualify(operator, vp.stop_id),
            status=_STATUS.get(vp.current_status) if vp.HasField("current_status") else None,
            reported_at=vp.timestamp or feed.header.timestamp,
        ))

    if not vehicles:
        raise FeedError("VehiclePositions feed has no in-service vehicles")
    return VehiclePositions(timestamp=feed.header.timestamp, vehicles=tuple(vehicles), out_of_service=out_of_service)


def decode_service_alerts(payload: bytes, operator: str) -> ServiceAlerts:
    """Alerts, with their lines and stops.

    In the fixture every alert's ``informed_entity`` is an (agency, route, stop)
    triple, except one agency-only entity (SF_15898, the Folsom Street Fair
    reroutes). An agency-only alert has no lines and no stops, so it only
    appears when nothing is filtered on.

    Unlike the other two feeds, an alerts feed with no entities is accepted:
    "no alerts" is a real state. Only a payload with no header timestamp, which
    is what an empty or truncated body parses to, is refused.

    Alert ids are 511's entity ids verbatim: all 41 already carry the operator
    (``SF_15874``).
    """
    feed = _parse(payload)
    alerts: list[AlertInfo] = []
    for entity in feed.entity:
        if not entity.HasField("alert"):
            continue
        alert = entity.alert
        lines: dict[str, None] = {}
        stops: dict[str, None] = {}
        for informed in alert.informed_entity:
            # An entity naming another agency is about that agency's service.
            if informed.agency_id and informed.agency_id != operator:
                continue
            if (line := _qualify(operator, informed.route_id)) is not None:
                lines[line] = None
            if (stop := _qualify(operator, informed.stop_id)) is not None:
                stops[stop] = None
        alerts.append(AlertInfo(
            id=entity.id,
            header=_text(alert.header_text) or "",
            description=_text(alert.description_text) or "",
            periods=tuple((p.start or None, p.end or None) for p in alert.active_period),
            lines=tuple(lines),
            stops=tuple(stops),
            url=_text(alert.url),
        ))
    return ServiceAlerts(timestamp=feed.header.timestamp, alerts=tuple(alerts))


DECODERS = {
    "tripupdates": decode_trip_updates,
    "vehiclepositions": decode_vehicle_positions,
    "servicealerts": decode_service_alerts,
}


# MARK: - Helpers


def _parse(payload: bytes) -> pb.FeedMessage:
    # Stored fixtures are gzipped, and a proxy could hand over a body still
    # compressed. The magic number is unambiguous: a FeedMessage starts with the
    # header's tag, 0x0a.
    if payload[:2] == b"\x1f\x8b":
        try:
            payload = gzip.decompress(payload)
        except (OSError, EOFError) as error:
            raise FeedError(f"bad gzip: {error}") from None
    feed = pb.FeedMessage()
    try:
        feed.ParseFromString(payload)
    except DecodeError as error:
        raise FeedError(f"not a GTFS-Realtime feed: {error}") from None
    # Zero bytes, or an HTML error page that happens to parse, decode as a
    # FeedMessage with no header. Every real 511 feed has a timestamp.
    if not feed.header.timestamp:
        raise FeedError("feed has no header timestamp")
    return feed


def _qualify(operator: str, upstream: str) -> str | None:
    """``SF:<id>``, or None where the upstream id could not be a valid ref.

    Checked here because every id (stop, line, trip, vehicle) is validated again
    when the response is built, and one bad id would otherwise fail a whole
    response. It is also what lets a client split any of them at the first colon
    to ask 511 directly (API.md, section 3).
    """
    if not upstream or any(c in upstream for c in ",:/") or any(c.isspace() for c in upstream):
        return None
    return ref(operator, upstream)


def _text(translated: pb.TranslatedString) -> str | None:
    """English if there is any, else the untagged text, else whatever is first.
    511 sends English only (all 41 fixture alerts), so this is future-proofing
    that costs one loop."""
    if not translated.translation:
        return None
    by_language = {t.language: t.text for t in translated.translation}
    return by_language.get("en") or by_language.get("") or translated.translation[0].text
