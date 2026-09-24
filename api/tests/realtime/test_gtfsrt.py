"""The facts ``app.upstream.gtfsrt`` is built on, measured on the recorded feeds.

The numbers are exact for these recordings. If 511 changes behaviour, a new
recording will fail here first, which is the point.
"""

import gzip

import pytest
from google.transit import gtfs_realtime_pb2 as pb
from pydantic import TypeAdapter
from rt_support import FIXTURES, fixture_bytes

from app.models.ids import TripId

from app.upstream.gtfsrt import (
    FeedError,
    decode_service_alerts,
    decode_trip_updates,
    decode_vehicle_positions,
)

DEPARTURE_ONLY_STOPS = {
    "SF:13243", "SF:14824", "SF:15063", "SF:15223", "SF:15240", "SF:16063", "SF:16575", "SF:16644",
    "SF:16932", "SF:17164", "SF:17398", "SF:17778", "SF:17919", "SF:18109", "SF:18153",
}


@pytest.fixture(scope="module")
def trips():
    return decode_trip_updates(fixture_bytes("tripupdates-1.pb.gz"), "SF")


@pytest.fixture(scope="module")
def vehicles():
    return decode_vehicle_positions(fixture_bytes("vehiclepositions-1.pb.gz"), "SF")


# MARK: - TripUpdates


def test_trip_updates_shape(trips):
    assert trips.timestamp == 1790113897
    assert len(trips.trips) == 1435
    assert sum(len(t.stops) for t in trips.trips) == 36137
    assert all(t.id.startswith("SF:") and t.line.startswith("SF:") for t in trips.trips)
    assert all(s.stop.startswith("SF:") for t in trips.trips for s in t.stops)


def test_departure_only_updates_are_at_trip_starts(trips):
    at = [(t, i) for t in trips.trips for i, s in enumerate(t.stops) if s.kind == "departure"]
    assert len(at) == 1032
    assert sum(1 for _, i in at if i == 0) == 1027
    # The other five are route 9 trips whose first two stops are both
    # departure-only: still the start of the trip, just a two-stop start.
    later = [(t, i) for t, i in at if i != 0]
    assert len(later) == 5
    assert {(t.line, i, t.stops[i].stop, t.stops[0].kind) for t, i in later} == {
        ("SF:9", 1, "SF:13243", "departure")
    }


def test_departure_only_stops_would_vanish_from_an_arrival_only_scan(trips):
    arriving = {s.stop for t in trips.trips for s in t.stops if s.kind == "arrival"}
    departing = {s.stop for t in trips.trips for s in t.stops if s.kind == "departure"}
    assert departing - arriving == DEPARTURE_ONLY_STOPS


def test_terminates_is_the_last_update_when_it_is_an_arrival(trips):
    terminating = [(t, s) for t in trips.trips for s in t.stops if s.terminates]
    assert len(terminating) == 1424
    assert all(s is t.stops[-1] and s.kind == "arrival" for t, s in terminating)
    # The 11 others are one-update trips whose only update is a departure: the
    # trip starts there. "Last update" alone would call that a terminus.
    single = [t for t in trips.trips if not t.stops[-1].terminates]
    assert len(single) == 11
    assert all(len(t.stops) == 1 and t.stops[0].kind == "departure" for t in single)


def test_every_trip_update_names_its_vehicle(trips):
    assert all(t.vehicle and t.vehicle.startswith("SF:") for t in trips.trips)
    assert len({t.vehicle for t in trips.trips}) == 532


def test_second_recording_agrees(trips):
    second = decode_trip_updates(fixture_bytes("tripupdates-2.pb.gz"), "SF")
    assert second.timestamp - trips.timestamp == 17
    assert len(second.trips) == 1434
    assert sum(1 for t in second.trips for s in t.stops if s.kind == "departure") == 1032


def test_times_within_a_trip_never_go_backwards(trips):
    # The Worker relied on this to stop scanning a trip early; nothing here
    # depends on it, but a board sorted per platform would look wrong if it broke.
    assert all([s.time for s in t.stops] == sorted(s.time for s in t.stops) for t in trips.trips)


# MARK: - VehiclePositions


def test_out_of_service_vehicles_are_left_out(vehicles):
    assert len(vehicles.vehicles) + vehicles.out_of_service == 677
    assert vehicles.out_of_service == 145  # 21%: no trip at all
    assert all(v.trip.startswith("SF:") and v.line.startswith("SF:") for v in vehicles.vehicles)


def test_zero_bearing_and_speed_are_unknown(vehicles):
    raw = pb.FeedMessage()
    raw.ParseFromString(fixture_bytes("vehiclepositions-1.pb.gz"))
    assert sum(1 for e in raw.entity if e.vehicle.position.bearing == 0) == 148  # 22% of all 677
    assert sum(1 for e in raw.entity if e.vehicle.position.speed == 0) == 406
    assert sum(1 for v in vehicles.vehicles if v.bearing is None) == 58
    assert sum(1 for v in vehicles.vehicles if v.speed is None) == 272
    assert all(v.bearing != 0 and v.speed != 0 for v in vehicles.vehicles)


def test_vehicle_fields(vehicles):
    v = next(v for v in vehicles.vehicles if v.id == "SF:10")
    assert (v.line, v.trip, v.direction, v.stop, v.status) == ("SF:PH", "SF:12136860_M11", 1, "SF:15081", "stoppedAt")
    assert v.reported_at == 1790113881
    # current_status is read with HasField: its default would claim "inTransitTo".
    assert sum(1 for v in vehicles.vehicles if v.status is None) == 2


def test_vehicle_positions_agree_with_trip_updates(trips, vehicles):
    by_trip = {t.id: t.vehicle for t in trips.trips}
    shared = [v for v in vehicles.vehicles if v.trip in by_trip]
    assert len(shared) == 515
    assert all(by_trip[v.trip] == v.id for v in shared)


# MARK: - Ids


def test_every_realtime_id_is_a_ref(trips, vehicles):
    # The contract a 511 fallback relies on (API.md, section 3): split at the first
    # colon, and the rest is exactly 511's id.
    refs = TypeAdapter(TripId)
    for t in trips.trips:
        refs.validate_python(t.id)
        if t.vehicle is not None:
            refs.validate_python(t.vehicle)
    for v in vehicles.vehicles:
        refs.validate_python(v.id)
        refs.validate_python(v.trip)


def _trip_feed(trip_id: str, vehicle_id: str) -> bytes:
    message = pb.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    message.header.timestamp = 1790113897
    for i, (trip, vehicle) in enumerate([("12134484_M11", "2019"), (trip_id, vehicle_id)]):
        entity = message.entity.add(id=str(i))
        tu = entity.trip_update
        tu.trip.trip_id = trip
        tu.trip.route_id = "L"
        tu.trip.direction_id = 1
        tu.vehicle.id = vehicle
        update = tu.stop_time_update.add(stop_id="16992")
        update.arrival.time = 1790113947
        position = message.entity.add(id=f"v{i}").vehicle
        position.trip.trip_id = trip
        position.trip.route_id = "L"
        position.vehicle.id = vehicle
        position.position.latitude = 37.79
        position.position.longitude = -122.39
    return message.SerializeToString()


@pytest.mark.parametrize("bad", ["a:b", "a/b", "a,b", "a b"])
def test_a_trip_id_that_cannot_be_a_ref_is_dropped(bad):
    payload = _trip_feed(bad, "2070")
    assert [t.id for t in decode_trip_updates(payload, "SF").trips] == ["SF:12134484_M11"]
    positions = decode_vehicle_positions(payload, "SF")
    assert [v.id for v in positions.vehicles] == ["SF:2019"]
    assert positions.out_of_service == 1


@pytest.mark.parametrize("bad", ["a:b", "a/b", "a,b", "a b"])
def test_a_vehicle_id_that_cannot_be_a_ref_is_unknown(bad):
    payload = _trip_feed("12133095_M11", bad)
    assert [(t.id, t.vehicle) for t in decode_trip_updates(payload, "SF").trips] == [
        ("SF:12134484_M11", "SF:2019"), ("SF:12133095_M11", None),
    ]  # fmt: skip
    assert [v.id for v in decode_vehicle_positions(payload, "SF").vehicles] == ["SF:2019"]


# MARK: - ServiceAlerts


def test_alerts():
    feed = decode_service_alerts(fixture_bytes("servicealerts.pb.gz"), "SF")
    assert len(feed.alerts) == 41
    assert all(a.active_at(feed.timestamp) for a in feed.alerts)
    moved = next(a for a in feed.alerts if a.id == "SF_15874")
    assert (moved.lines, moved.stops) == (("SF:9",), ("SF:13240",))
    assert moved.header.startswith("9 STOP TEMP. MOVED")
    assert moved.periods == ((1789974000, 1792479599),)
    agency_wide = next(a for a in feed.alerts if a.id == "SF_15898")
    assert (agency_wide.lines, agency_wide.stops) == ((), ())


def test_alert_period_bounds():
    feed = decode_service_alerts(fixture_bytes("servicealerts.pb.gz"), "SF")
    moved = next(a for a in feed.alerts if a.id == "SF_15874")
    assert not moved.active_at(1789974000 - 1)
    assert moved.active_at(1789974000)
    assert moved.active_at(1792479599)
    assert not moved.active_at(1792479600)


# MARK: - Refusals


def _header_only() -> bytes:
    message = pb.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    message.header.timestamp = 1790113897
    return message.SerializeToString()


@pytest.mark.parametrize("payload", [b"", b"<html>Service Unavailable</html>", b"\x1f\x8bnot gzip"])
@pytest.mark.parametrize("decode", [decode_trip_updates, decode_vehicle_positions, decode_service_alerts])
def test_garbage_is_refused(decode, payload):
    with pytest.raises(FeedError):
        decode(payload, "SF")


@pytest.mark.parametrize("decode", [decode_trip_updates, decode_vehicle_positions])
def test_an_empty_feed_is_refused(decode):
    with pytest.raises(FeedError):
        decode(_header_only(), "SF")


def test_an_empty_alerts_feed_is_real():
    assert decode_service_alerts(_header_only(), "SF").alerts == ()


def test_gzipped_payloads_are_accepted():
    raw = (FIXTURES / "servicealerts.pb.gz").read_bytes()
    assert raw[:2] == b"\x1f\x8b"
    assert len(decode_service_alerts(raw, "SF").alerts) == 41
    assert gzip.decompress(raw)[:1] == b"\x0a"
