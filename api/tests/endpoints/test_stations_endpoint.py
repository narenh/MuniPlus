"""``/api/stations`` and ``/api/stations/{id}`` against tests/fixtures/transit."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.data import loader
from app.data.network import Network
from app.models.api import Alert, AlertsResponse, StationDetailResponse, StationsResponse
from app.models.curation import Platform
from app.endpoints.stations import etag_of

FIXTURE = Path(__file__).parent.parent / "fixtures" / "transit"
VERSION = "0123456789abcdef0123456789abcdef01234567"


def test_list(client):
    r = client.get("/api/v1/stations")
    assert r.status_code == 200
    body = r.json()
    StationsResponse.model_validate(body)
    assert body["version"] == VERSION
    assert body["formerIds"] == {"mongomery": "montgomery"}
    assert [s["id"] for s in body["stations"]] == [
        "castroPlaza", "churchMarket", "clayDrumm", "embarcadero",
        "montgomery", "powell", "powellMarket", "unionSquare",
    ]  # fmt: skip
    church = next(s for s in body["stations"] if s["id"] == "churchMarket")
    assert church["lines"] == ["SF:J", "SF:F"]
    assert church["modes"] == ["metro", "streetcar"]
    assert church["platforms"][0] == {"id": "SF:17073", "heading": "northbound", "lines": ["SF:J"], "stops": ["SF:17073"], "formerIds": []}


def test_a_schema_change_changes_the_etag(client, monkeypatch):
    # Same data, new response shape: a client must not be told its old body is current.
    from app.endpoints import stations as st
    before = client.get("/api/v1/stations").headers["etag"]
    monkeypatch.setattr(st, "schema_hash", lambda: "different")
    assert client.get("/api/v1/stations", headers={"If-None-Match": before}).status_code == 200


def test_list_etag(client):
    r = client.get("/api/v1/stations")
    assert r.headers["etag"] == etag_of(VERSION)
    assert r.headers["cache-control"] == "no-cache"

    again = client.get("/api/v1/stations", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == etag_of(VERSION)

    tag = etag_of(VERSION).strip('"')
    for header in (f'W/"{tag}"', tag, f'"old", "{tag}"', "*"):
        assert client.get("/api/v1/stations", headers={"If-None-Match": header}).status_code == 304
    assert client.get("/api/v1/stations", headers={"If-None-Match": '"old"'}).status_code == 200


def test_a_new_version_changes_the_etag(make_app):
    app = make_app()
    client = TestClient(app)
    etag = client.get("/api/v1/stations").headers["etag"]
    app.state.network = loader.load(FIXTURE, "fedcba")
    r = client.get("/api/v1/stations", headers={"If-None-Match": etag})
    assert r.status_code == 200
    assert r.json()["version"] == "fedcba"


def test_detail(client):
    r = client.get("/api/v1/stations/montgomery")
    assert r.status_code == 200
    body = r.json()
    StationDetailResponse.model_validate(body)
    assert body["version"] == VERSION
    m = body["station"]
    assert m["name"] == "Montgomery"
    assert m["operators"] == ["SF", "BA"]
    assert m["subways"] == [{"id": "marketStreetSubway", "name": "Market Subway"}]
    assert m["alerts"] == []
    assert m["platforms"][0] == {
        "id": "SF:15731",
        "heading": "eastbound",
        "lines": ["SF:J", "SF:K", "SF:L", "SF:M", "SF:N"],
        "stops": ["SF:15731"],
        "formerIds": [],
        "name": None,
        "stopName": "Metro Montgomery Station/Downtown",
        "lat": 37.789219,
        "lon": -122.401351,
    }
    # Editorial fields stay out of the public API.
    assert "note" not in m and "verified" not in m and "formerIds" not in m


def test_detail_is_never_cached(client):
    # It carries live alerts: no ETag to revalidate against, and nothing kept.
    assert client.get("/api/v1/stations/montgomery").headers["cache-control"] == "no-store"


def test_detail_has_no_etag(client):
    # Alerts change with the realtime feed, not with the version.
    assert "etag" not in client.get("/api/v1/stations/montgomery").headers


def test_transfers_serve_both_stations(client):
    powell = client.get("/api/v1/stations/powell").json()["station"]
    union = client.get("/api/v1/stations/unionSquare").json()["station"]
    assert {"to": "unionSquare", "name": "Union Square", "mode": "indoor"} in powell["transfers"]
    assert union["transfers"] == [{"to": "powell", "name": "Powell", "mode": "indoor"}]
    assert {"to": "powellMarket", "name": "Powell & Market", "mode": "street"} in powell["transfers"]


def test_former_id_redirects(client):
    r = client.get("/api/v1/stations/mongomery", follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"] == "/api/v1/stations/montgomery"
    followed = client.get("/api/v1/stations/mongomery")
    assert followed.status_code == 200
    assert followed.json()["station"]["id"] == "montgomery"


def test_redirect_keeps_the_query(client):
    r = client.get("/api/v1/stations/mongomery?x=1", follow_redirects=False)
    assert r.headers["location"] == "/api/v1/stations/montgomery?x=1"


def test_unknown_station(client):
    for sid in ("nowhere", "Montgomery", "SF:15731"):
        r = client.get(f"/api/v1/stations/{sid}")
        assert r.status_code == 404
        assert r.json()["error"] == "not-found"
        assert sid in r.json()["message"]


def test_station_with_no_live_platforms_is_not_served(make_app):
    curation, snapshots = loader.read(FIXTURE)
    curation.stations.stations["clayDrumm"].platforms = [Platform(id="SF:99999", heading="westbound")]
    client = TestClient(make_app(Network(curation, snapshots, VERSION)))
    assert "clayDrumm" not in [s["id"] for s in client.get("/api/v1/stations").json()["stations"]]
    assert client.get("/api/v1/stations/clayDrumm").status_code == 404


class FakeRealtime:
    """Stands in for track C's ``Realtime``: only ``alerts`` is used here."""

    def __init__(self):
        self.calls = []

    def alerts(self, *, lines=None, stations=None, platforms=None):
        self.calls.append({"lines": lines, "stations": stations, "platforms": platforms})
        alert = Alert(
            id="a1",
            header="Powell elevator out of service",
            description="",
            active_periods=[],
            lines=[],
            stops=["SF:15417"],
            stations=["powell"],
            url=None,
        )
        return AlertsResponse(refresh_after=60, fetched_at=1_790_000_000, feed_at=None, alerts=[alert])


def test_detail_alerts_from_realtime(make_app):
    realtime = FakeRealtime()
    client = TestClient(make_app(realtime=realtime))
    station = client.get("/api/v1/stations/powell").json()["station"]
    assert realtime.calls == [{"lines": None, "stations": {"powell"}, "platforms": None}]
    assert [a["id"] for a in station["alerts"]] == ["a1"]
    assert station["alerts"][0]["activePeriods"] == []
    # The shared model is not changed by a request.
    assert client.app.state.network.station("powell").alerts == []
