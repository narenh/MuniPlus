"""The snapshot refresh: fetch from a mock relay, drift, commit.

The Canopy Transit Relay is an ``httpx.MockTransport`` serving a GTFS zip built
here from the fixture snapshot, so a feed "unchanged" from the fixture differs from it only where a test
says so. Nothing here reaches the network.
"""

import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest

from transit_editor.editor.refresh import PENDING_DIR, Refresher
from transit_editor.models import files
from transit_editor.models.ids import upstream_of

from editor_world import FIXTURE, edit_file, git

RELAY = "https://relay.test"
NOW = 1_790_150_400.0  # 2026-09-23 08:00 UTC

NEW_FROM, NEW_TO = "20270116", "20270601"


# MARK: - A synthetic 511


def fixture_feed() -> dict:
    """The fixture snapshot as a feed spec: stops, routes, patterns and shapes, keyed
    by 511's own ids (no ``SF:``), in a form a test can edit before zipping."""
    snap = files.read_snapshot(FIXTURE, "SF")
    return {
        "period": (NEW_FROM, NEW_TO),
        "stops": {upstream_of(pid): [s.name, s.lat, s.lon] for pid, s in snap.stops.root.items()},
        "routes": {
            upstream_of(lid): {
                "short": line.short_name,
                "long": line.long_name,
                "type": line.route_type,
                "color": (line.color or "").lstrip("#"),
                "text": (line.text_color or "").lstrip("#"),
                "mode": line.mode,
            }
            for lid, line in snap.lines.root.items()
        },
        "patterns": [
            [
                upstream_of(lid), p.direction, p.headsign, p.trips, [upstream_of(s) for s in p.stops],
                p.shape and upstream_of(p.shape),
            ]
            for lid, patterns in snap.patterns.root.items()
            for p in patterns
        ],
        "shapes": {upstream_of(sid): points for sid, points in snap.shapes.root.items()},
    }  # fmt: skip


def zip_feed(feed: dict) -> tuple[bytes, bytes]:
    """(GTFS zip, /transit/lines JSON). Every trip runs on one day, so a pattern's
    ``trips`` in the built snapshot is the number of trip rows written for it."""
    start, end = feed["period"]
    stops = ["stop_id,stop_code,stop_name,stop_lat,stop_lon,location_type"]
    stops += [f'{sid},{sid},"{name}",{lat},{lon},' for sid, (name, lat, lon) in feed["stops"].items()]
    routes = ["route_id,agency_id,route_short_name,route_long_name,route_type,route_color,route_text_color"]
    routes += [
        f'{rid},SF,{r["short"]},"{r["long"]}",{r["type"]},{r["color"]},{r["text"]}' for rid, r in feed["routes"].items()
    ]
    trips = ["route_id,service_id,trip_id,trip_headsign,direction_id,shape_id"]
    times = ["trip_id,arrival_time,departure_time,stop_id,stop_sequence"]
    n = 0
    for rid, direction, headsign, count, seq, shape in feed["patterns"]:
        for _ in range(count):
            n += 1
            trips.append(f'{rid},day,t{n},"{headsign}",{direction},{shape or ""}')
            times += [f"t{n},08:{i:02d}:00,08:{i:02d}:00,{sid},{i + 1}" for i, sid in enumerate(seq)]
    tables = {
        "stops.txt": stops,
        "routes.txt": routes,
        "trips.txt": trips,
        "stop_times.txt": times,
        "feed_info.txt": ["feed_publisher_name,feed_start_date,feed_end_date", f"511 SF Bay,{start},{end}"],
        "calendar_dates.txt": ["service_id,date,exception_type", f"day,{start},1"],
        "shapes.txt": ["shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence"]
        + [f"{sid},{lat},{lon},{i + 1}" for sid, points in feed["shapes"].items() for i, (lon, lat) in enumerate(points)],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, rows in tables.items():
            zf.writestr(name, "\n".join(rows) + "\n")
    lines = [{"Id": rid, "Name": r["long"], "TransportMode": r["mode"]} for rid, r in feed["routes"].items()]
    # 511 sends a byte order mark.
    return buf.getvalue(), b"\xef\xbb\xbf" + json.dumps(lines).encode()


class Upstream:
    """A fake relay serving one feed, counting every request that reaches it."""

    def __init__(self, feed: dict):
        self.requests: list[httpx.Request] = []
        self.failure: httpx.Response | None = None
        self.serve(feed)

    def serve(self, feed: dict) -> None:
        self.zip, self.lines = zip_feed(feed)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.failure is not None:
            return self.failure
        if request.url.path == "/static/datafeeds":
            return httpx.Response(200, content=self.zip, headers={"Content-Type": "application/zip"})
        if request.url.path == "/static/lines":
            return httpx.Response(200, content=self.lines, headers={"Content-Type": "application/json"})
        return httpx.Response(404)


class Clock:
    def __init__(self, t: float = NOW):
        self.t = t

    def __call__(self) -> float:
        return self.t


def wire(world, upstream: Upstream, clock: Clock | None = None) -> None:
    world.app.state.snapshot_refresh = Refresher(
        world.settings, transport=httpx.MockTransport(upstream), clock=clock or Clock()
    )


@pytest.fixture
def keyed(make):
    world = make(relay_url=RELAY)
    world.login()
    return world


def fetch(world, **body):
    return world.client.post("/editor/api/snapshot/fetch", json=body)


def commit(world, pending: str, base: str):
    return world.client.post("/editor/api/snapshot/commit", json={"pending": pending, "baseVersion": base})


def drifted() -> dict:
    """The fixture feed with one or more of every kind of drift."""
    feed = fixture_feed()
    stops, routes, patterns = feed["stops"], feed["routes"], feed["patterns"]
    # Clay & Drumm's only pole goes, and with it the 1, its only line.
    del stops["14015"]
    del routes["1"]
    patterns[:] = [p for p in patterns if p[0] != "1"]
    # A new pole on the K, at a corner no station has.
    stops["99001"] = ["Ocean Ave & Lee Ave", 37.72303, -122.45355]
    k0 = next(p for p in patterns if p[0] == "K" and p[1] == 0)
    k0[4] = [*k0[4], "99001"]
    # Castro Plaza's pole moves about 50 m north; Market & Powell is renamed.
    stops["13311"][1] += 0.00045
    stops["15688"][0] = "Market St & Powell St (Cable Car Turnaround)"
    # A new line, the J recoloured, the 5 renamed.
    routes["714"] = dict(short="714", long="BART EARLY BIRD", type=3, color="666666", text="FFFFFF", mode="bus")
    patterns.append(["714", 0, "Daly City BART", 5, ["15688", "16064"], None])
    routes["J"]["color"] = "FF0000"
    routes["5"]["long"] = "FULTON STREET"
    # The J's outbound diagram skips Powell.
    j0 = next(p for p in patterns if p[0] == "J" and p[1] == 0)
    j0[4] = [s for s in j0[4] if s != "16995"]
    # The F's path up Market is resurveyed.
    feed["shapes"]["F1"][1] = [-122.43514, 37.76263]
    return feed


# MARK: - Fetch


def test_fetch_reports_every_kind_of_drift_and_commits_nothing(keyed):
    upstream = Upstream(drifted())
    wire(keyed, upstream)
    res = fetch(keyed)
    assert res.status_code == 200, res.text
    body = res.json()

    # Exactly two calls to the relay, for SF, with no key of ours.
    assert [str(r.url.copy_with(query=None)) for r in upstream.requests] == [
        f"{RELAY}/static/datafeeds",
        f"{RELAY}/static/lines",
    ]
    assert [dict(r.url.params) for r in upstream.requests] == [{"operator": "SF"}, {"operator": "SF"}]

    assert body["baseVersion"] == keyed.seed
    assert body["fetchedAt"] == "2026-09-23T08:00:00Z"
    assert body["unchanged"] is False
    drift = body["drift"]
    assert drift["operator"] == "SF"
    assert drift["old"] == {"serviceFrom": "2026-08-29", "serviceTo": "2027-01-15"}
    assert drift["new"] == {"serviceFrom": "2027-01-16", "serviceTo": "2027-06-01"}
    assert drift["sameZip"] is False

    assert [s["stop"] for s in drift["stopsAdded"]] == ["SF:99001"]
    assert drift["stopsRemoved"] == [
        {"stop": "SF:14015", "name": "Clay St & Drumm St", "lat": 37.79532, "lon": -122.397473}
    ]
    (moved,) = drift["stopsMoved"]
    assert (moved["stop"], moved["metres"]) == ("SF:13311", 50)
    assert (moved["oldLat"], moved["lat"]) == (37.762576, pytest.approx(37.763026))
    assert drift["stopsRenamed"] == [
        {
            "stop": "SF:15688",
            "oldName": "Market St & Powell St",
            "name": "Market St & Powell St (Cable Car Turnaround)",
        }
    ]

    assert drift["linesAdded"] == [{"line": "SF:714", "shortName": "714", "longName": "BART EARLY BIRD", "mode": "bus"}]
    assert [l["line"] for l in drift["linesRemoved"]] == ["SF:1"]
    assert drift["linesChanged"] == [
        {"line": "SF:5", "changes": [{"field": "longName", "old": "FULTON", "new": "FULTON STREET"}]},
        {"line": "SF:J", "changes": [{"field": "color", "old": "#FAA633", "new": "#FF0000"}]},
    ]

    changed = {(p["line"], p["direction"]): p for p in drift["patternsChanged"]}
    assert set(changed) == {("SF:J", 0), ("SF:K", 0)}
    assert (changed["SF:J", 0]["stopsAdded"], changed["SF:J", 0]["stopsRemoved"]) == ([], ["SF:16995"])
    assert changed["SF:J", 0]["old"]["stops"] == ["SF:17217", "SF:16994", "SF:16995", "SF:18059"]
    assert changed["SF:J", 0]["new"]["stops"] == ["SF:17217", "SF:16994", "SF:18059"]
    assert (changed["SF:K", 0]["stopsAdded"], changed["SF:K", 0]["stopsRemoved"]) == (["SF:99001"], [])
    # The N's two direction-1 patterns in the fixture differ only in headsign, and
    # the build merges them; its most-run pattern is the same, so it is not listed.

    assert drift["deadStops"] == [
        {"stop": "SF:14015", "platform": "SF:14015", "station": "clayDrumm", "stationName": "Clay & Drumm",
         "heading": "westbound"}
    ]
    assert drift["deadStations"] == [{"station": "clayDrumm", "name": "Clay & Drumm", "stops": ["SF:14015"]}]
    # SF:15418 is already in the queue and SF:13510 is ignored: only the new stop.
    (new,) = drift["newUnassigned"]
    assert (new["stop"], new["name"], new["lines"]) == ("SF:99001", "Ocean Ave & Lee Ave", ["SF:K"])
    assert new["proposal"]["newStation"]["id"] == "oceanLee"
    assert new["proposal"]["heading"] is not None

    # Nothing committed or written in the checkout; the pending snapshot is on disk
    # outside it.
    assert keyed.head() == keyed.remote_head() == keyed.seed
    assert git(keyed.checkout, "status", "--porcelain") == ""
    pending = keyed.settings.data_dir / PENDING_DIR
    assert not pending.is_relative_to(keyed.checkout)
    assert files.read_snapshot(pending, "SF").meta.service_from.isoformat() == "2027-01-16"
    assert not list(pending.rglob("*.zip"))


def test_no_relay_refuses_without_calling(make):
    world = make(relay_url="")
    world.login()
    upstream = Upstream(fixture_feed())
    wire(world, upstream)
    res = fetch(world)
    assert res.status_code == 503
    assert res.json()["error"] == "unavailable"
    assert "RELAY_URL" in res.json()["message"]
    assert upstream.requests == []


def test_a_relay_error_is_passed_on_and_keeps_nothing(keyed):
    upstream = Upstream(fixture_feed())
    upstream.failure = httpx.Response(
        503,
        json={"error": "unavailable", "message": "No copy of 511's datafeeds for SF: 511 answered 500."},
    )
    wire(keyed, upstream)
    res = fetch(keyed)
    assert res.status_code == 503
    message = res.json()["message"]
    assert "the relay answered 503 for datafeeds" in message
    assert "511 answered 500" in message
    assert [r.url.path for r in upstream.requests] == ["/static/datafeeds"]
    assert not (keyed.settings.data_dir / PENDING_DIR).exists()


def test_an_unreachable_relay_is_a_503(keyed):
    def down(request):
        raise httpx.ConnectError("connection refused", request=request)

    keyed.app.state.snapshot_refresh = Refresher(keyed.settings, transport=httpx.MockTransport(down), clock=Clock())
    res = fetch(keyed)
    assert res.status_code == 503
    assert "could not reach the relay" in res.json()["message"]


def test_the_relay_url_may_end_in_a_slash(make):
    world = make(relay_url=RELAY + "/")
    world.login()
    upstream = Upstream(fixture_feed())
    wire(world, upstream)
    assert fetch(world).status_code == 200
    assert [r.url.path for r in upstream.requests] == ["/static/datafeeds", "/static/lines"]


def test_needs_a_session_and_json(make):
    world = make(relay_url=RELAY)
    upstream = Upstream(fixture_feed())
    wire(world, upstream)
    assert fetch(world).status_code == 401
    world.login()
    res = world.client.post("/editor/api/snapshot/fetch", content=b"", headers={"Content-Type": "text/plain"})
    assert res.status_code == 415
    assert upstream.requests == []


def test_an_empty_body_and_an_unknown_operator(keyed):
    upstream = Upstream(fixture_feed())
    wire(keyed, upstream)
    res = keyed.client.post("/editor/api/snapshot/fetch", headers={"Content-Type": "application/json"})
    assert res.status_code == 200, res.text
    assert fetch(keyed, operator="BA").status_code == 400
    assert len(upstream.requests) == 2


def test_a_feed_that_does_not_build_keeps_nothing(keyed):
    upstream = Upstream(fixture_feed())
    upstream.zip = b'{"error": "not a zip"}'
    wire(keyed, upstream)
    res = fetch(keyed)
    assert res.status_code == 503
    assert "could not be built" in res.json()["message"]
    data = keyed.settings.data_dir
    assert not (data / PENDING_DIR).exists()
    assert [p.name for p in data.iterdir() if PENDING_DIR in p.name] == []


def test_a_new_fetch_replaces_the_pending_one(keyed):
    upstream = Upstream(drifted())
    wire(keyed, upstream)
    first = fetch(keyed).json()["pending"]
    second = fetch(keyed).json()["pending"]
    assert first != second
    res = commit(keyed, first, keyed.seed)
    assert res.status_code == 404
    assert res.json()["error"] == "not-found"
    assert keyed.head() == keyed.seed


# MARK: - Commit


def changed_paths(repo: Path, commit: str) -> list[str]:
    return sorted(git(repo, "show", "--format=", "--name-only", commit).splitlines())


def test_commit_writes_only_the_snapshot_and_swaps_the_network(keyed):
    wire(keyed, Upstream(drifted()))
    old_network = keyed.app.state.network
    fetched = fetch(keyed).json()
    res = commit(keyed, fetched["pending"], fetched["baseVersion"])
    assert res.status_code == 200, res.text
    body = res.json()
    sha = body["commit"]
    assert sha == body["version"] == keyed.head() == keyed.remote_head()
    assert (body["pushed"], body["pushError"]) == (True, None)
    assert git(keyed.checkout, "rev-parse", f"{sha}^") == keyed.seed
    assert git(keyed.checkout, "show", "--no-patch", "--format=%an|%s", sha) == (
        "Editor|Refresh SF snapshot for service 2027-01-16 to 2027-06-01"
    )
    assert changed_paths(keyed.checkout, sha) == [
        "snapshot/SF/lines.json",
        "snapshot/SF/meta.json",
        "snapshot/SF/patterns.json",
        "snapshot/SF/shapes.json",
        "snapshot/SF/stops.json",
    ]
    assert git(keyed.checkout, "status", "--porcelain") == ""
    # The warnings the drift predicted are now the checkout's.
    warnings = {(w["code"], w.get("stop") or w.get("station")) for w in body["validation"]["warnings"]}
    assert {("stop-not-in-snapshot", "SF:14015"), ("station-has-no-live-platforms", "clayDrumm"),
            ("unassigned-stop", "SF:99001")} <= warnings  # fmt: skip

    # The public network is replaced whole, at the new version.
    network = keyed.app.state.network
    assert network is not old_network and network.version == sha
    assert network.station("clayDrumm") is None
    assert old_network.station("clayDrumm") is not None
    assert network.line("SF:714") is not None
    assert keyed.client.get("/editor/api/review").json()["version"] == sha

    # The pending snapshot is gone.
    assert not (keyed.settings.data_dir / PENDING_DIR).exists()
    assert commit(keyed, fetched["pending"], sha).status_code == 404


def test_an_identical_snapshot_commits_nothing(keyed):
    clock = Clock()
    wire(keyed, Upstream(drifted()), clock)
    fetched = fetch(keyed).json()
    first = commit(keyed, fetched["pending"], fetched["baseVersion"]).json()["commit"]
    network = keyed.app.state.network

    # The same zip an hour later: only fetchedAt differs.
    clock.t += 3600
    again = fetch(keyed).json()
    assert again["baseVersion"] == first
    assert (again["unchanged"], again["drift"]["sameZip"]) == (True, True)
    drift = again["drift"]
    for key in ("stopsAdded", "stopsRemoved", "stopsMoved", "stopsRenamed", "linesAdded", "linesRemoved",
                "linesChanged", "patternsChanged", "deadStops", "deadStations", "newUnassigned"):  # fmt: skip
        assert drift[key] == [], key
    res = commit(keyed, again["pending"], again["baseVersion"])
    assert res.status_code == 200, res.text
    assert (res.json()["commit"], res.json()["version"]) == (None, first)
    assert keyed.head() == keyed.remote_head() == first
    assert git(keyed.checkout, "status", "--porcelain") == ""
    assert keyed.app.state.network is network
    assert not (keyed.settings.data_dir / PENDING_DIR).exists()


def test_a_curation_edit_since_the_fetch_is_not_a_conflict(keyed):
    wire(keyed, Upstream(drifted()))
    fetched = fetch(keyed).json()
    theirs = edit_file(keyed.other, "curation/lines.json", '"color": "#FAA633"', '"color": "#FAA634"', "Recolour J")
    res = commit(keyed, fetched["pending"], fetched["baseVersion"])
    assert res.status_code == 200, res.text
    sha = res.json()["commit"]
    assert git(keyed.checkout, "rev-parse", f"{sha}^") == theirs
    assert keyed.remote_head() == sha
    assert '"#FAA634"' in (keyed.checkout / "curation/lines.json").read_text()
    assert keyed.app.state.network.version == sha


def test_another_refresh_since_the_fetch_is_a_conflict(keyed):
    wire(keyed, Upstream(drifted()))
    fetched = fetch(keyed).json()
    theirs = edit_file(
        keyed.other, "snapshot/SF/meta.json", "2026-09-22T21:48:36Z", "2026-09-23T09:00:00Z", "Their refresh"
    )
    res = commit(keyed, fetched["pending"], fetched["baseVersion"])
    assert res.status_code == 409
    body = res.json()
    assert (body["error"], body["paths"], body["version"]) == ("conflict", ["snapshot/SF/meta.json"], theirs)
    # Fast-forwarded to theirs, nothing of ours written, and the pending snapshot is
    # kept: fetching again is the fix, but nothing was lost by refusing.
    assert keyed.head() == keyed.remote_head() == theirs
    assert git(keyed.checkout, "status", "--porcelain") == ""
    assert keyed.app.state.network.version == theirs
    assert (keyed.settings.data_dir / PENDING_DIR).exists()


def test_a_newer_base_version_does_not_hide_another_refresh(keyed):
    wire(keyed, Upstream(drifted()))
    fetched = fetch(keyed).json()
    edit_file(keyed.other, "snapshot/SF/meta.json", "2026-09-22T21:48:36Z", "2026-09-23T09:00:00Z", "Their refresh")
    later = edit_file(keyed.other, "curation/stations.json", '"Castro Plaza"', '"Castro Square"', "Rename")
    res = commit(keyed, fetched["pending"], later)
    assert res.status_code == 409
    assert res.json()["paths"] == ["snapshot/SF/meta.json"]


def test_a_remote_that_moves_during_the_commit_is_rebased_and_retried(keyed, monkeypatch):
    wire(keyed, Upstream(drifted()))
    fetched = fetch(keyed).json()
    checkout = keyed.app.state.editor.checkout
    real, calls, theirs = checkout.fetch, [], []

    def fetch_then_race():
        real()
        calls.append(1)
        if len(calls) == 1:
            # Another editor saves between our fetch and our push.
            theirs.append(
                edit_file(keyed.other, "curation/stations.json", '"Castro Plaza"', '"Castro Square"', "Rename")
            )

    monkeypatch.setattr(checkout, "fetch", fetch_then_race)
    res = commit(keyed, fetched["pending"], fetched["baseVersion"])
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(calls) == 2  # the commit's fetch, and the retry's
    assert body["pushed"] is True
    assert body["commit"] == keyed.head() == keyed.remote_head()
    assert git(keyed.checkout, "rev-parse", f"{body['commit']}^") == theirs[0]
    assert changed_paths(keyed.checkout, body["commit"])[0].startswith("snapshot/SF/")
    network = keyed.app.state.network
    assert network.version == body["commit"]
    assert network.station("castroPlaza").name == "Castro Square"
    assert network.line("SF:714") is not None


def test_commit_refuses_when_read_only(make):
    world = make(relay_url=RELAY, git_author_email="")
    world.login()
    wire(world, Upstream(drifted()))
    fetched = fetch(world).json()
    res = commit(world, fetched["pending"], fetched["baseVersion"])
    assert res.status_code == 503
    assert "read-only" in res.json()["message"]
    assert world.head() == world.seed


def test_the_schema_lists_the_new_endpoints(keyed):
    paths = keyed.app.openapi()["paths"]
    assert {"/editor/api/review", "/editor/api/snapshot/fetch", "/editor/api/snapshot/commit"} <= set(paths)
