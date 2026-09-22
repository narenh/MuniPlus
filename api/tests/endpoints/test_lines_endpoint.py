"""``/api/lines`` and ``/api/lines/{id}`` against tests/fixtures/transit."""

from app.models.api import LineDetailResponse, LinesResponse

VERSION = "0123456789abcdef0123456789abcdef01234567"


def test_list(client):
    r = client.get("/api/lines")
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
    }
    assert [line["id"] for line in body["lines"]][:7] == ["SF:J", "SF:K", "SF:L", "SF:M", "SF:N", "SF:T", "SF:F"]
    assert "directions" not in lines["SF:J"]


def test_list_etag(client):
    r = client.get("/api/lines")
    assert r.headers["etag"] == f'"{VERSION}"'
    assert client.get("/api/lines", headers={"If-None-Match": r.headers["etag"]}).status_code == 304


def test_detail(client):
    r = client.get("/api/lines/SF:F")
    assert r.status_code == 200
    body = r.json()
    LineDetailResponse.model_validate(body)
    f = body["line"]
    assert (f["name"], f["mode"]) == ("F Market & Wharves", "streetcar")
    assert f["directions"] == [
        {"direction": 0, "headsign": "Castro", "stations": ["churchMarket"], "platforms": ["SF:15661"]},
        {
            "direction": 1,
            "headsign": "Fisherman's Wharf",
            "stations": ["castroPlaza", "churchMarket"],
            "platforms": ["SF:13311", "SF:15662"],
        },
    ]


def test_detail_most_run_pattern(client):
    n = client.get("/api/lines/SF:N").json()["line"]
    assert [d["headsign"] for d in n["directions"]] == ["Ocean Beach", "Caltrain"]


def test_detail_etag(client):
    r = client.get("/api/lines/SF:J")
    assert r.headers["etag"] == f'"{VERSION}"'
    assert client.get("/api/lines/SF:J", headers={"If-None-Match": r.headers["etag"]}).status_code == 304


def test_unknown_line(client):
    for line_id in ("SF:Q", "J", "SF:j"):
        r = client.get(f"/api/lines/{line_id}")
        assert r.status_code == 404
        assert r.json() == {"error": "not-found", "message": f"No line {line_id!r}."}
