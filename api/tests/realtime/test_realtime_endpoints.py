"""``/api/arrivals``, ``/api/vehicles``, ``/api/alerts`` on their own app."""

import time
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rt_support import FakeNetwork, fixture_bytes, make_settings

from app.db import Database
from app.endpoints import alerts, arrivals, vehicles
from app.realtime.pollers import Pollers
from app.realtime.state import Realtime

NOW = 1790113897


def make_app(realtime: Realtime | None) -> FastAPI:
    app = FastAPI()
    for module in (arrivals, vehicles, alerts):
        app.include_router(module.router)
    if realtime is not None:
        app.state.realtime = realtime
    return app


@pytest.fixture
def client(settings, db):
    network = FakeNetwork(stations={"SF:13240": "eleventhMission"}, headsigns={("SF:2", 0): "Sutter/Clement"})
    realtime = Realtime(lambda: network, settings, db=db, clock=lambda: NOW)
    realtime.ingest("SF", "tripupdates", fixture_bytes("tripupdates-1.pb.gz"), fetched_at=NOW)
    realtime.ingest("SF", "vehiclepositions", fixture_bytes("vehiclepositions-1.pb.gz"), fetched_at=NOW)
    realtime.ingest("SF", "servicealerts", fixture_bytes("servicealerts.pb.gz"), fetched_at=NOW)
    return TestClient(make_app(realtime))


def assert_problem(response, status=400):
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error", "message"} and body["message"]
    assert response.headers["cache-control"] == "no-store"
    return body


# MARK: - Arrivals


def test_arrivals(client):
    response = client.get("/api/arrivals", params={"platforms": "SF:15621,SF:13243,SF:99999"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["fetchedAt"] == NOW
    assert list(body["platforms"]) == ["SF:15621", "SF:13243", "SF:99999"]
    assert len(body["platforms"]["SF:15621"]) == 6
    assert body["platforms"]["SF:99999"] == []
    first = body["platforms"]["SF:15621"][0]
    assert set(first) == {"line", "direction", "headsign", "time", "kind", "terminates", "trip", "vehicle"}
    assert {a["kind"] for a in body["platforms"]["SF:13243"]} == {"departure"}


def test_arrivals_limit(client):
    def count(limit):
        return len(client.get("/api/arrivals", params={"platforms": "SF:15621", "limit": limit}).json()["platforms"]["SF:15621"])

    assert count("1") == 1
    assert count("1000") == arrivals.MAX_LIMIT  # capped, not refused


def test_duplicate_platforms_are_answered_once(client):
    body = client.get("/api/arrivals", params={"platforms": "SF:15621,SF:15621"}).json()
    assert list(body["platforms"]) == ["SF:15621"]


@pytest.mark.parametrize("params", [
    {},
    {"platforms": ""},
    {"platforms": "15621"},
    {"platforms": "sf:15621"},
    {"platforms": "SF:15621,"},
    {"platforms": "SF:1/2"},
    {"platforms": ",".join(f"SF:{n}" for n in range(51))},
    {"platforms": "SF:15621", "limit": "abc"},
    {"platforms": "SF:15621", "limit": "0"},
    {"platforms": "SF:15621", "limit": "-3"},
])
def test_arrivals_bad_requests(client, params):
    assert_problem(client.get("/api/arrivals", params=params))


def test_fifty_platforms_is_allowed(client):
    assert client.get("/api/arrivals", params={"platforms": ",".join(f"SF:{n}" for n in range(50))}).status_code == 200


# MARK: - Vehicles


def test_vehicles(client):
    response = client.get("/api/vehicles")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert len(body["vehicles"]) == 532
    assert set(body["vehicles"][0]) == {
        "id", "line", "direction", "trip", "lat", "lon", "bearing", "speed", "stop", "status", "reportedAt"
    }
    only = client.get("/api/vehicles", params={"line": "SF:N,SF:PH"}).json()["vehicles"]
    assert only and {v["line"] for v in only} == {"SF:N", "SF:PH"}


@pytest.mark.parametrize("line", ["N", "SF:N,bad", "SF:"])
def test_vehicles_bad_line(client, line):
    assert_problem(client.get("/api/vehicles", params={"line": line}))


# MARK: - Alerts


def test_alerts(client):
    response = client.get("/api/alerts")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert len(response.json()["alerts"]) == 41
    by_station = client.get("/api/alerts", params={"station": "eleventhMission"}).json()["alerts"]
    assert [a["id"] for a in by_station] == ["SF_15874", "SF_15898"]  # plus the agency-wide one
    assert by_station[0]["stations"] == ["eleventhMission"]
    assert set(by_station[0]) == {"id", "header", "description", "activePeriods", "lines", "platforms", "stations", "url"}
    assert "SF_15874" in {a["id"] for a in client.get("/api/alerts", params={"line": "SF:9"}).json()["alerts"]}
    assert "SF_15874" in {a["id"] for a in client.get("/api/alerts", params={"platforms": "SF:13240"}).json()["alerts"]}


@pytest.mark.parametrize("params", [{"line": "9"}, {"station": "eleventh-mission"}, {"platforms": "13240"}])
def test_alerts_bad_requests(client, params):
    assert_problem(client.get("/api/alerts", params=params))


# MARK: - Without realtime


def test_no_realtime_is_a_503_but_bad_input_is_still_a_400():
    client = TestClient(make_app(None))
    assert assert_problem(client.get("/api/vehicles"), 503)["error"] == "unavailable"
    assert_problem(client.get("/api/arrivals", params={"platforms": "SF:1"}), 503)
    assert_problem(client.get("/api/arrivals"), 400)


# MARK: - Wired as app/main.py will wire it


def test_lifespan_wiring_in_fixtures_mode(tmp_path):
    settings = make_settings(tmp_path, fixtures=True)

    @asynccontextmanager
    async def lifespan(app):
        db = Database(settings.db_path)
        app.state.realtime = Realtime(lambda: getattr(app.state, "network", None), settings, db=db)
        pollers = Pollers(app.state.realtime)
        await pollers.start()
        yield
        await pollers.stop()
        db.close()

    app = FastAPI(lifespan=lifespan)
    for module in (arrivals, vehicles, alerts):
        app.include_router(module.router)

    with TestClient(app) as client:
        deadline = time.monotonic() + 5
        while not client.get("/api/vehicles").json()["vehicles"]:
            assert time.monotonic() < deadline, "fixtures were never replayed"
            time.sleep(0.02)
        board = client.get("/api/arrivals", params={"platforms": "SF:15621"}).json()
        assert board["platforms"]["SF:15621"]
        assert {a["headsign"] for a in board["platforms"]["SF:15621"]} <= {"Outbound", "Inbound"}
        assert len(client.get("/api/alerts").json()["alerts"]) == 41
