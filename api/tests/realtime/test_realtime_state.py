"""``Realtime``: queries, the swap, and health. The clock is always the feed's."""

import pytest
from google.transit import gtfs_realtime_pb2 as pb
from rt_support import FakeNetwork, fixture_bytes, make_settings

from app.realtime.state import Realtime

TU1, TU2 = fixture_bytes("tripupdates-1.pb.gz"), fixture_bytes("tripupdates-2.pb.gz")
VP1 = fixture_bytes("vehiclepositions-1.pb.gz")
ALERTS = fixture_bytes("servicealerts.pb.gz")
NOW = 1790113897
"""tripupdates-1's header timestamp."""

BUSIEST = "SF:15621"


@pytest.fixture
def loaded(realtime):
    assert realtime.ingest("SF", "tripupdates", TU1, fetched_at=NOW + 1)
    assert realtime.ingest("SF", "vehiclepositions", VP1, fetched_at=NOW + 1)
    assert realtime.ingest("SF", "servicealerts", ALERTS, fetched_at=NOW - 199)
    return realtime


# MARK: - Arrivals


def test_every_requested_platform_is_present(loaded):
    response = loaded.arrivals([BUSIEST, "SF:13243", "SF:99999999", "BA:EMBR"], limit=6, now=NOW)
    assert list(response.platforms) == [BUSIEST, "SF:13243", "SF:99999999", "BA:EMBR"]
    assert response.platforms["SF:99999999"] == []
    assert response.platforms["BA:EMBR"] == []  # an operator we do not poll
    assert response.fetched_at == NOW + 1


def test_arrivals_are_upcoming_sorted_and_cut(loaded):
    arrivals = loaded.arrivals([BUSIEST], limit=6, now=NOW).platforms[BUSIEST]
    assert len(arrivals) == 6
    times = [a.time for a in arrivals]
    assert times == sorted(times) and times[0] >= NOW
    everything = loaded.arrivals([BUSIEST], limit=1000, now=NOW).platforms[BUSIEST]
    assert everything[:6] == arrivals
    later = loaded.arrivals([BUSIEST], limit=6, now=times[2]).platforms[BUSIEST]
    assert later[0].time >= times[2] and later[0] == arrivals[2]


def test_the_past_is_dropped(loaded):
    # 94 stop times in the feed are already behind its own header.
    upcoming = loaded.arrivals([BUSIEST], limit=1000, now=NOW + 3600).platforms[BUSIEST]
    assert all(a.time >= NOW + 3600 for a in upcoming)
    assert loaded.arrivals([BUSIEST], limit=6, now=NOW + 10**6).platforms[BUSIEST] == []


def test_departure_only_stops_are_served_as_departures(loaded):
    arrivals = loaded.arrivals(["SF:13243"], limit=50, now=NOW).platforms["SF:13243"]
    assert arrivals and all(a.kind == "departure" and not a.terminates for a in arrivals)


def test_terminates(loaded):
    # The fixture patterns end the inbound J, K and L at SF:16992 (Embarcadero)
    # and start the outbound ones at SF:17217. In this recording 37 metro trips
    # end at the first and 38 start at the second.
    ending = loaded.arrivals(["SF:16992"], limit=50, now=NOW).platforms["SF:16992"]
    assert any(a.terminates for a in ending)
    assert all(a.kind == "arrival" for a in ending if a.terminates)
    starting = loaded.arrivals(["SF:17217"], limit=50, now=NOW).platforms["SF:17217"]
    assert any(a.kind == "departure" for a in starting)
    assert not any(a.terminates for a in starting if a.kind == "departure")


def test_arrival_ids_are_qualified(loaded):
    a = loaded.arrivals([BUSIEST], limit=1, now=NOW).platforms[BUSIEST][0]
    assert a.line.startswith("SF:") and a.trip.startswith("SF:") and a.vehicle.startswith("SF:")


def test_headsign_from_network_else_direction_name(settings, db):
    network = FakeNetwork(headsigns={("SF:2", 0): "Sutter/Clement"})
    realtime = Realtime(lambda: network, settings, db=db)
    realtime.ingest("SF", "tripupdates", TU1, fetched_at=NOW)
    arrivals = [a for v in realtime.arrivals(["SF:16501", "SF:13243"], limit=50, now=NOW).platforms.values() for a in v]
    assert {a.headsign for a in arrivals if (a.line, a.direction) == ("SF:2", 0)} == {"Sutter/Clement"}
    assert {a.headsign for a in arrivals if a.line == "SF:9"} <= {"Outbound", "Inbound"}
    assert {a.headsign for a in arrivals if a.line == "SF:9" and a.direction == 0} == {"Outbound"}


def test_no_network_yet_still_serves_arrivals(settings, db):
    realtime = Realtime(lambda: None, settings, db=db)
    realtime.ingest("SF", "tripupdates", TU1, fetched_at=NOW)
    assert realtime.arrivals([BUSIEST], limit=3, now=NOW).platforms[BUSIEST]


def test_the_network_is_asked_afresh_each_time(settings, db):
    # The editor replaces the network whole; realtime must not hold on to the old one.
    current = {"network": FakeNetwork(headsigns={("SF:2", 0): "Old"})}
    realtime = Realtime(lambda: current["network"], settings, db=db)
    realtime.ingest("SF", "tripupdates", TU1, fetched_at=NOW)
    first = realtime.arrivals(["SF:16501"], limit=1, now=NOW).platforms["SF:16501"][0]
    current["network"] = FakeNetwork(headsigns={(first.line, first.direction): "New"})
    assert realtime.arrivals(["SF:16501"], limit=1, now=NOW).platforms["SF:16501"][0].headsign == "New"


def test_vehicle_falls_back_to_vehicle_positions(realtime):
    message = pb.FeedMessage()
    message.ParseFromString(TU1)
    for entity in message.entity:
        entity.trip_update.ClearField("vehicle")
    realtime.ingest("SF", "tripupdates", message.SerializeToString(), fetched_at=NOW)

    before = realtime.arrivals([BUSIEST], limit=30, now=NOW).platforms[BUSIEST]
    assert all(a.vehicle is None for a in before)

    realtime.ingest("SF", "vehiclepositions", VP1, fetched_at=NOW)
    after = realtime.arrivals([BUSIEST], limit=30, now=NOW).platforms[BUSIEST]
    positioned = {v.trip: v.id for v in realtime.vehicles().vehicles}
    assert any(a.vehicle for a in after)
    assert all(a.vehicle == positioned.get(a.trip) for a in after)


# MARK: - Swapping


def test_a_bad_payload_keeps_the_last_good_state(loaded):
    good = loaded.arrivals([BUSIEST], limit=6, now=NOW)
    assert not loaded.ingest("SF", "tripupdates", b"<html>502 Bad Gateway</html>", fetched_at=NOW + 60)
    assert loaded.arrivals([BUSIEST], limit=6, now=NOW) == good
    assert loaded.fetched_at("SF", "tripupdates") == NOW + 1
    feeds, _ = loaded.health(now=NOW + 60)
    assert feeds["SF:tripupdates"].last_error


def test_an_empty_payload_keeps_the_last_good_state(loaded):
    empty = pb.FeedMessage()
    empty.header.gtfs_realtime_version = "2.0"
    empty.header.timestamp = NOW + 60
    for feed in ("tripupdates", "vehiclepositions"):
        assert not loaded.ingest("SF", feed, empty.SerializeToString(), fetched_at=NOW + 60)
    assert loaded.arrivals([BUSIEST], limit=1, now=NOW).platforms[BUSIEST]
    assert len(loaded.vehicles().vehicles) == 532


def test_a_good_payload_replaces_and_clears_the_error(loaded):
    loaded.ingest("SF", "tripupdates", b"junk", fetched_at=NOW + 10)
    assert loaded.ingest("SF", "tripupdates", TU2, fetched_at=NOW + 18)
    feeds, _ = loaded.health(now=NOW + 20)
    assert feeds["SF:tripupdates"].last_error is None
    assert feeds["SF:tripupdates"].fetched_at == NOW + 18
    assert feeds["SF:tripupdates"].age_seconds == 2


# MARK: - Vehicles


def test_vehicles(loaded):
    everything = loaded.vehicles()
    assert len(everything.vehicles) == 532
    assert everything.fetched_at == NOW + 1
    only = loaded.vehicles({"SF:N", "SF:PH"})
    assert only.vehicles and {v.line for v in only.vehicles} == {"SF:N", "SF:PH"}
    assert loaded.vehicles(set()).vehicles == []
    assert sum(1 for v in everything.vehicles if v.bearing is None) == 58


# MARK: - Alerts


def test_alerts_active_now(loaded):
    assert len(loaded.alerts(now=NOW).alerts) == 41
    assert loaded.alerts(now=1789974000 - 23032097 - 1).alerts == []
    # The last alert to end ends at NOW + 8,676,702 (inclusive).
    assert loaded.alerts(now=NOW + 8676703).alerts == []
    assert loaded.alerts(now=NOW).fetched_at == NOW - 199


def test_alert_filters(settings, db):
    network = FakeNetwork(stations={"SF:13240": "eleventhMission"})
    realtime = Realtime(lambda: network, settings, db=db)
    realtime.ingest("SF", "servicealerts", ALERTS, fetched_at=NOW)

    def ids(**filters):
        return {a.id for a in realtime.alerts(now=NOW, **filters).alerts}

    assert "SF_15874" in ids(lines={"SF:9"})
    assert "SF_15874" in ids(platforms={"SF:13240"})
    # The agency-wide alert (SF_15898) names no line or platform: it affects every
    # station, so it rides along with every filtered answer.
    assert ids(stations={"eleventhMission"}) == {"SF_15874", "SF_15898"}
    assert ids(lines={"SF:NOPE"}, stations={"eleventhMission"}) == {"SF_15874", "SF_15898"}  # any filter may match
    assert ids(lines=set()) == {"SF_15898"}
    assert "SF_15898" in ids()
    assert "SF_15898" in ids(lines={"SF:9"})

    moved = next(a for a in realtime.alerts(now=NOW, lines={"SF:9"}).alerts if a.id == "SF_15874")
    assert moved.stations == ["eleventhMission"]
    assert moved.model_dump()["activePeriods"] == [{"start": 1789974000, "end": 1792479599}]


# MARK: - Health and the clock


def test_health(loaded, db):
    db.reserve_call("SF", "tripupdates", NOW - 10, per_hour=55)
    call = db.reserve_call("SF", "vehiclepositions", NOW - 5, per_hour=55)
    db.finish_call(call, status=200, rate_limit_remaining=17, error=None)
    db.reserve_call("SF", "servicealerts", NOW - 4000, per_hour=55)  # outside the hour

    feeds, budget = loaded.health(now=NOW + 101)
    assert set(feeds) == {"SF:tripupdates", "SF:vehiclepositions", "SF:servicealerts"}
    assert feeds["SF:tripupdates"].interval_seconds == 180
    assert feeds["SF:servicealerts"].interval_seconds == 1200
    assert feeds["SF:vehiclepositions"].age_seconds == 100
    assert (budget.per_hour, budget.used_last_hour, budget.last_rate_limit_remaining) == (55, 2, 17)


def test_health_before_any_data(realtime):
    feeds, budget = realtime.health(now=NOW)
    assert all(f.fetched_at is None and f.age_seconds is None for f in feeds.values())
    assert budget.used_last_hour == 0


def test_fixtures_mode_uses_the_feeds_own_clock(tmp_path, db):
    # The recordings are from 2026-09-22; against a later wall clock every
    # arrival in them has passed, and a local FIXTURES=1 run would show nothing.
    settings = make_settings(tmp_path, fixtures=True)
    realtime = Realtime(lambda: None, settings, db=db, clock=lambda: NOW + 10**7)
    realtime.ingest("SF", "tripupdates", TU1, fetched_at=NOW)
    realtime.ingest("SF", "servicealerts", ALERTS, fetched_at=NOW)
    assert realtime.arrivals([BUSIEST], limit=6).platforms[BUSIEST]
    assert len(realtime.alerts().alerts) == 41


def test_live_mode_uses_the_wall_clock(settings, db):
    realtime = Realtime(lambda: None, settings, db=db, clock=lambda: NOW + 10**7)
    realtime.ingest("SF", "tripupdates", TU1, fetched_at=NOW)
    assert realtime.arrivals([BUSIEST], limit=6).platforms[BUSIEST] == []
