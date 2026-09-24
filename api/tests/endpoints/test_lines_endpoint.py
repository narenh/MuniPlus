"""``/api/lines``, ``/api/lines/{id}`` and ``/api/shapes`` against tests/fixtures/transit."""

from app.models.api import LineDetailResponse, LinesResponse, ShapesResponse
from app.endpoints.stations import etag_of

VERSION = "0123456789abcdef0123456789abcdef01234567"


def test_list(client):
    r = client.get("/api/v1/lines")
    assert r.status_code == 200
    body = r.json()
    LinesResponse.model_validate(body)
    assert body["version"] == VERSION
    lines = {line["id"]: line for line in body["lines"]}
    assert lines["SF:F"]["mode"] == "streetcar"
    assert lines["SF:LOWL"] == {
        "id": "SF:LOWL",
        "shortName": "LOWL",
        "name": "LOWL Owl Taraval",
        "color": "#666666",
        "textColor": "#FFFFFF",
        "mode": "bus",
        "hidden": False,
        "replaces": ["SF:L"],
        "owl": True,
    }
    assert not lines["SF:L"]["owl"]
    assert [line["id"] for line in body["lines"]][:7] == ["SF:J", "SF:K", "SF:L", "SF:M", "SF:N", "SF:T", "SF:F"]
    assert "directions" not in lines["SF:J"]


def test_list_etag(client):
    r = client.get("/api/v1/lines")
    assert r.headers["etag"] == etag_of(VERSION)
    assert client.get("/api/v1/lines", headers={"If-None-Match": r.headers["etag"]}).status_code == 304


def test_detail(client):
    r = client.get("/api/v1/lines/SF:F")
    assert r.status_code == 200
    body = r.json()
    LineDetailResponse.model_validate(body)
    f = body["line"]
    assert (f["name"], f["mode"]) == ("F Market & Wharves", "streetcar")
    assert f["directions"] == [
        {"direction": 0, "headsign": "Castro", "stations": ["churchMarket"], "stops": ["SF:15661"], "shape": None},
        {
            "direction": 1,
            "headsign": "Fisherman's Wharf",
            "stations": ["castroPlaza", "churchMarket"],
            "stops": ["SF:13311", "SF:15662"],
            "shape": "SF:F1",
        },
    ]


def test_detail_most_run_pattern(client):
    n = client.get("/api/v1/lines/SF:N").json()["line"]
    assert [d["headsign"] for d in n["directions"]] == ["Ocean Beach", "Caltrain"]


def test_detail_etag(client):
    r = client.get("/api/v1/lines/SF:J")
    assert r.headers["etag"] == etag_of(VERSION)
    assert client.get("/api/v1/lines/SF:J", headers={"If-None-Match": r.headers["etag"]}).status_code == 304


def test_unknown_line(client):
    for line_id in ("SF:Q", "J", "SF:j"):
        r = client.get(f"/api/v1/lines/{line_id}")
        assert r.status_code == 404
        assert r.json() == {"error": "not-found", "message": f"No line {line_id!r}."}


# MARK: - Shapes


def test_shapes(client):
    r = client.get("/api/v1/shapes")
    assert r.status_code == 200
    body = r.json()
    ShapesResponse.model_validate(body)
    # The fixture's point halfway along Market is on the line and is dropped; the
    # corner at Castro is not.
    assert body == {
        "shapes": {
            "SF:F1": [[-122.434979, 37.762576], [-122.435139, 37.76262], [-122.429214, 37.76725]],
        }
    }


def test_shapes_etag_is_the_shapes_not_the_version(client):
    r = client.get("/api/v1/shapes")
    etag = r.headers["etag"]
    assert etag != etag_of(VERSION)
    assert r.headers["cache-control"] == "no-cache"
    assert client.get("/api/v1/shapes", headers={"If-None-Match": etag}).status_code == 304
    assert client.get("/api/v1/shapes", headers={"If-None-Match": etag_of(VERSION)}).status_code == 200
