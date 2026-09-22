"""What the realtime endpoints answer from.

``Realtime`` holds the last good decode of each feed, per operator, and builds
responses from it. Each successful decode builds fresh indexes and swaps them in
with one assignment, so a request sees either the old feed or the new one, never
half of each. A payload that fails to decode, or decodes to nothing, leaves the
previous state in place and is recorded as ``last_error``: arrival times are
absolute, so an older feed still counts down correctly on the client, and
``fetchedAt`` tells it how old the estimate is. Blanking a good board because one
poll failed is the failure the Worker's comments warn about.

Joins to the network (headsigns, stations) happen per request through
``network()``, never at decode time, because the editor replaces the network
whole and a decoded feed must not carry the old one's answers.

Wiring, for ``app/main.py``::

    db = Database(settings.db_path)
    realtime = Realtime(lambda: app.state.network, settings, db=db)
    pollers = Pollers(realtime)      # app.realtime.pollers
    # lifespan: await pollers.start() ... await pollers.stop()
    app.state.realtime = realtime
"""

import bisect
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal, Protocol

from app.db import Database
from app.models.api import (
    ActivePeriod,
    Alert,
    AlertsResponse,
    Arrival,
    ArrivalsResponse,
    BudgetHealth,
    FeedHealth,
    Vehicle,
    VehiclesResponse,
)
from app.models.ids import operator_of
from app.settings import Settings
from app.upstream.gtfsrt import (
    DECODERS,
    FEEDS,
    AlertInfo,
    Feed,
    FeedError,
    ServiceAlerts,
    StopTime,
    Trip,
    TripUpdates,
    VehiclePositions,
)

log = logging.getLogger(__name__)


class NetworkView(Protocol):
    """The two things realtime needs from the loaded network. Track B's
    ``Network`` satisfies this structurally, without importing it."""

    def station_of(self, platform_id: str) -> str | None: ...

    def headsign(self, line_id: str, direction: int) -> str | None: ...


DIRECTION_NAMES: dict[int, str] = {0: "Outbound", 1: "Inbound"}
"""The headsign when the network has none for a line and direction (a line new to
511 since the snapshot, or one with no pattern that way).

SFMTA's GTFS uses direction 0 for outbound and 1 for inbound, which the fixture
patterns bear out: direction 0 heads to Ocean Beach, Balboa Park, SF Zoo, Castro;
direction 1 to Embarcadero and Fisherman's Wharf. "Outbound" is less useful than
a destination, but it is true, where the alternatives were an empty string on
the board or dropping a real prediction. The trip's last stop would be better
still, but ``NetworkView`` has no stop names to say it with."""


@dataclass(frozen=True, slots=True)
class _Event:
    time: int
    stop: StopTime
    trip: Trip


@dataclass(frozen=True, slots=True)
class _Arrivals:
    fetched_at: int
    feed_time: int
    by_platform: dict[str, list[_Event]]
    """Each list sorted by time, so "from now" is a bisect."""
    times: dict[str, list[int]]


@dataclass(frozen=True, slots=True)
class _Vehicles:
    fetched_at: int
    feed_time: int
    feed: VehiclePositions
    by_trip: dict[str, str]
    """trip -> vehicle, for arrivals whose TripUpdate names no vehicle."""


@dataclass(frozen=True, slots=True)
class _Alerts:
    fetched_at: int
    feed_time: int
    alerts: tuple[AlertInfo, ...]


_State = _Arrivals | _Vehicles | _Alerts


class Realtime:
    def __init__(
        self,
        network: Callable[[], NetworkView | None],
        settings: Settings,
        *,
        db: Database | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._network = network
        self.settings = settings
        self.db = db if db is not None else Database(settings.db_path)
        self._clock = clock
        self._state: dict[tuple[str, Feed], _State] = {}
        self._errors: dict[tuple[str, Feed], str | None] = {}

    # MARK: - Ingest

    def ingest(self, operator: str, feed: Feed, payload: bytes, fetched_at: int) -> bool:
        """Decode a payload and, if it is good, swap it in. Returns whether it was."""
        try:
            decoded = DECODERS[feed](payload, operator)
            state = _build(decoded, fetched_at)
        except FeedError as error:
            self.record_error(operator, feed, str(error))
            return False
        except Exception as error:  # a decoder bug must not take the poller down
            log.exception("decoding %s %s failed", operator, feed)
            self.record_error(operator, feed, f"{type(error).__name__}: {error}")
            return False
        self._state[(operator, feed)] = state
        self._errors[(operator, feed)] = None
        return True

    def record_error(self, operator: str, feed: Feed, message: str) -> None:
        self._errors[(operator, feed)] = message
        log.warning("%s %s: %s (keeping the previous copy)", operator, feed, message)

    def fetched_at(self, operator: str, feed: Feed) -> int | None:
        state = self._state.get((operator, feed))
        return state.fetched_at if state else None

    # MARK: - Queries

    def arrivals(self, platform_ids: list[str], limit: int, *, now: int | None = None) -> ArrivalsResponse:
        """The next ``limit`` arrivals at each platform, at or after ``now``.

        Every requested platform is in the answer, as an empty list when nothing
        is due or the id is unknown. The vehicle is the TripUpdate's own, else the
        one VehiclePositions places on the same trip."""
        network = self._network()
        platforms: dict[str, list[Arrival]] = {}
        fetched: list[int] = []
        for platform in platform_ids:
            operator = operator_of(platform)
            index = self._state.get((operator, "tripupdates"))
            positions = self._state.get((operator, "vehiclepositions"))
            if not isinstance(index, _Arrivals):
                platforms[platform] = []
                continue
            fetched.append(index.fetched_at)
            at = self._now(now, index)
            events = index.by_platform.get(platform, [])
            start = bisect.bisect_left(index.times.get(platform, []), at)
            platforms[platform] = [
                Arrival(
                    line=event.trip.line,
                    direction=event.trip.direction,
                    headsign=_headsign(network, event.trip.line, event.trip.direction),
                    time=event.time,
                    kind=event.stop.kind,
                    terminates=event.stop.terminates,
                    trip=event.trip.id,
                    vehicle=event.trip.vehicle
                    or (positions.by_trip.get(event.trip.id) if isinstance(positions, _Vehicles) else None),
                )
                for event in events[start:start + limit]
            ]
        return ArrivalsResponse(fetched_at=min(fetched) if fetched else None, platforms=platforms)

    def vehicles(self, lines: set[str] | None = None) -> VehiclesResponse:
        """In-service vehicles, optionally only those on ``lines``."""
        out: list[Vehicle] = []
        fetched: list[int] = []
        for (operator, feed), state in self._state.items():
            if feed != "vehiclepositions" or not isinstance(state, _Vehicles):
                continue
            fetched.append(state.fetched_at)
            for v in state.feed.vehicles:
                if lines is not None and v.line not in lines:
                    continue
                out.append(Vehicle(
                    id=v.id, line=v.line, direction=v.direction, trip=v.trip, lat=v.lat, lon=v.lon,
                    bearing=v.bearing, speed=v.speed, stop=v.stop, status=v.status, reported_at=v.reported_at,
                ))
        out.sort(key=lambda v: (v.line, v.id))
        return VehiclesResponse(fetched_at=min(fetched) if fetched else None, vehicles=out)

    def alerts(
        self,
        *,
        lines: Iterable[str] | None = None,
        stations: Iterable[str] | None = None,
        platforms: Iterable[str] | None = None,
        now: int | None = None,
    ) -> AlertsResponse:
        """Alerts active at ``now``. With no filter, all of them; otherwise those
        naming any of ``lines`` or ``platforms``, or a platform of any of
        ``stations``, plus agency-wide alerts, which match every filter."""
        want_lines = set(lines) if lines is not None else None
        want_stations = set(stations) if stations is not None else None
        want_platforms = set(platforms) if platforms is not None else None
        unfiltered = want_lines is None and want_stations is None and want_platforms is None

        network = self._network()
        out: list[Alert] = []
        fetched: list[int] = []
        for (operator, feed), state in self._state.items():
            if feed != "servicealerts" or not isinstance(state, _Alerts):
                continue
            fetched.append(state.fetched_at)
            at = self._now(now, state)
            for alert in state.alerts:
                if not alert.active_at(at):
                    continue
                alert_stations = _stations(network, alert.platforms)
                # An alert naming no line and no stop is agency-wide (the fixture's
                # SF_15898, the Folsom Street Fair). It affects every station, so it
                # matches every filter; otherwise no station board could ever show it.
                agency_wide = not alert.lines and not alert.platforms
                if not unfiltered and not agency_wide and not (
                    (want_lines and want_lines.intersection(alert.lines))
                    or (want_platforms and want_platforms.intersection(alert.platforms))
                    or (want_stations and want_stations.intersection(alert_stations))
                ):
                    continue
                out.append(Alert(
                    id=alert.id,
                    header=alert.header,
                    description=alert.description,
                    active_periods=[ActivePeriod(start=s, end=e) for s, e in alert.periods],
                    lines=list(alert.lines),
                    platforms=list(alert.platforms),
                    stations=alert_stations,
                    url=alert.url,
                ))
        return AlertsResponse(fetched_at=min(fetched) if fetched else None, alerts=out)

    def health(self, *, now: int | None = None) -> tuple[dict[str, FeedHealth], BudgetHealth]:
        """Per feed, keyed ``<operator>:<feed>`` (``SF:tripupdates``), and the 511
        budget as the ledger has it. Ages are against the wall clock, even in
        fixtures mode: they are about how old our copy is, not the feed's world."""
        wall = int(self._clock()) if now is None else now
        feeds: dict[str, FeedHealth] = {}
        for operator in self.settings.operators:
            for feed in FEEDS:
                fetched_at = self.fetched_at(operator, feed)
                feeds[f"{operator}:{feed}"] = FeedHealth(
                    interval_seconds=self.interval(feed),
                    fetched_at=fetched_at,
                    age_seconds=max(0, wall - fetched_at) if fetched_at is not None else None,
                    last_error=self._errors.get((operator, feed)),
                )
        budget = BudgetHealth(
            per_hour=self.settings.budget_per_hour,
            used_last_hour=self.db.calls_since(wall - 3600),
            last_rate_limit_remaining=self.db.last_rate_limit_remaining(),
        )
        return feeds, budget

    def interval(self, feed: Feed) -> int:
        return {
            "tripupdates": self.settings.poll_arrivals_seconds,
            "vehiclepositions": self.settings.poll_vehicles_seconds,
            "servicealerts": self.settings.poll_alerts_seconds,
        }[feed]

    # MARK: - Clock

    def _now(self, now: int | None, state: _State) -> int:
        """The time to filter against. Explicit if given. In fixtures mode, the
        feed's own header time, because the recorded feeds are from 2026-09-22
        and against the wall clock every arrival in them is in the past and a
        local run would show empty boards. Otherwise the wall clock."""
        if now is not None:
            return now
        if self.settings.fixtures:
            return state.feed_time
        return int(self._clock())


# MARK: - Building

def _build(decoded: TripUpdates | VehiclePositions | ServiceAlerts, fetched_at: int) -> _State:
    match decoded:
        case TripUpdates():
            by_platform: dict[str, list[_Event]] = {}
            for trip in decoded.trips:
                for stop in trip.stops:
                    by_platform.setdefault(stop.platform, []).append(_Event(stop.time, stop, trip))
            for events in by_platform.values():
                events.sort(key=lambda e: (e.time, e.trip.line, e.trip.id))
            times = {platform: [e.time for e in events] for platform, events in by_platform.items()}
            return _Arrivals(fetched_at, decoded.timestamp, by_platform, times)
        case VehiclePositions():
            by_trip = {v.trip: v.id for v in decoded.vehicles}
            return _Vehicles(fetched_at, decoded.timestamp, decoded, by_trip)
        case ServiceAlerts():
            return _Alerts(fetched_at, decoded.timestamp, decoded.alerts)
    raise TypeError(type(decoded))


def _headsign(network: NetworkView | None, line: str, direction: Literal[0, 1]) -> str:
    headsign = network.headsign(line, direction) if network is not None else None
    return headsign or DIRECTION_NAMES[direction]


def _stations(network: NetworkView | None, platforms: Iterable[str]) -> list[str]:
    if network is None:
        return []
    return sorted({s for p in platforms if (s := network.station_of(p)) is not None})
