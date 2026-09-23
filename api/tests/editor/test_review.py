"""The review queue, against the fixture network in a local bare repo."""

import copy
import json

import pytest

from editor_world import commit_file, git


@pytest.fixture
def world(world):
    world.login()
    return world


def review(world) -> dict:
    res = world.client.get("/editor/api/review")
    assert res.status_code == 200, res.text
    return res.json()


def pull(world) -> None:
    git(world.checkout, "pull", "-q", "--ff-only")


def edit_json(world, rel: str, change, message: str) -> str:
    """Commit a change to one JSON file in the other working copy and push it."""
    git(world.other, "pull", "-q", "--ff-only", "origin", "main")
    doc = json.loads((world.other / rel).read_text())
    change(doc)
    return commit_file(world.other, rel, json.dumps(doc, indent=2) + "\n", message)


def test_the_fixture_queue(world):
    body = review(world)
    assert body["version"] == world.seed
    # SF:13510 is in the snapshot and in no station too, but ignored.json lists it.
    assert [u["platform"] for u in body["unassigned"]] == ["SF:15418"]
    stop = body["unassigned"][0]
    assert stop["stopName"] == "Balboa Park BART/Mezzanine Level"
    assert (stop["lat"], stop["lon"]) == (37.721809, -122.447425)
    # No pattern in the fixture stops there (on the real feed all 324 M trips do).
    assert stop["lines"] == []
    proposal = stop["proposal"]
    assert proposal["platform"] == "SF:15418"
    assert proposal["heading"] is None
    assert proposal["station"] is None
    assert proposal["newStation"]["id"] not in state_stations(world)
    assert proposal["newStation"]["name"]
    assert "no heading" in proposal["reason"]
    assert body["deadPlatforms"] == []
    assert body["deadStations"] == []


def state_stations(world) -> dict:
    return world.state()["curation"]["stations"]["stations"]


def test_needs_a_session(make):
    world = make()
    res = world.client.get("/editor/api/review")
    assert res.status_code == 401
    assert res.json()["error"] == "unauthorized"


def test_dead_platforms_and_stations(world):
    def add(doc):
        stations = doc["stations"]
        stations["castroPlaza"]["platforms"].append({"id": "SF:99998", "heading": "westbound"})
        stations["ghost"] = {"name": "Ghost", "platforms": [{"id": "SF:99999", "heading": "northbound"}]}

    edit_json(world, "curation/stations.json", add, "Platforms 511 does not list")
    pull(world)
    body = review(world)
    assert body["version"] == world.head() != world.seed
    assert body["deadPlatforms"] == [
        {"platform": "SF:99998", "station": "castroPlaza", "stationName": "Castro Plaza", "heading": "westbound"},
        {"platform": "SF:99999", "station": "ghost", "stationName": "Ghost", "heading": "northbound"},
    ]
    # Castro Plaza still has a live platform; Ghost has none.
    assert body["deadStations"] == [{"station": "ghost", "name": "Ghost", "platforms": ["SF:99999"]}]
    # The queue is the list form of the validator's warnings, so the two agree.
    warnings = world.state()["validation"]["warnings"]
    assert {w["platform"] for w in warnings if w["code"] == "platform-not-in-snapshot"} == {"SF:99998", "SF:99999"}
    assert {w["station"] for w in warnings if w["code"] == "station-has-no-live-platforms"} == {"ghost"}


def add_stops(world) -> str:
    """Three new 511 stops: one on the J's corner at Church & Market, and two
    poles at one new intersection, all served by the J."""
    new = {
        "SF:99100": {"name": "Church St & Market St", "lat": 37.7674, "lon": -122.42899},
        "SF:99200": {"name": "Noe St & 24th St", "lat": 37.75131, "lon": -122.43194},
        "SF:99201": {"name": "Noe St & 24th St", "lat": 37.75151, "lon": -122.43187},
    }
    edit_json(world, "snapshot/SF/stops.json", lambda doc: doc.update(new), "New stops")

    def serve(doc):
        # The J's inbound run, extended from Noe & 24th up to Church & Market.
        doc["SF:J"][1]["stops"] = ["SF:99201", "SF:99200", "SF:99100", *doc["SF:J"][1]["stops"]]

    return edit_json(world, "snapshot/SF/patterns.json", serve, "Serve them")


def test_proposals_join_a_station_or_share_a_new_one(world):
    add_stops(world)
    pull(world)
    queue = {u["platform"]: u for u in review(world)["unassigned"]}
    assert list(queue) == ["SF:15418", "SF:99100", "SF:99200", "SF:99201"]

    joins = queue["SF:99100"]
    assert joins["lines"] == ["SF:J"]
    assert joins["proposal"]["station"] == "churchMarket"
    assert joins["proposal"]["newStation"] is None
    assert joins["proposal"]["heading"] in {"northbound", "southbound", "eastbound", "westbound"}

    # One intersection, one new station for both poles: they were proposed together.
    a, b = queue["SF:99200"]["proposal"], queue["SF:99201"]["proposal"]
    assert a["station"] is None
    assert a["newStation"] == b["newStation"]
    assert a["newStation"]["id"] not in state_stations(world)
    # Nor the Balboa Park stop's new station.
    assert a["newStation"] != queue["SF:15418"]["proposal"]["newStation"]


def accept(curation: dict, stop: dict) -> dict:
    """What the frontend does to accept a proposal, per ``UnassignedStop``."""
    out = copy.deepcopy(curation)
    stations = out["stations"]["stations"]
    proposal = stop["proposal"]
    platform = {"id": stop["platform"], "heading": proposal["heading"] or "northbound"}
    if proposal["station"]:
        station = stations[proposal["station"]]
        station.pop("verified", None)
    else:
        new = proposal["newStation"]
        station = stations.setdefault(new["id"], {"name": new["name"], "platforms": []})
    station["platforms"].append(platform)
    return out


def test_accepting_every_proposal_with_a_normal_save_empties_the_queue(world):
    base = add_stops(world)
    pull(world)
    queue = review(world)["unassigned"]
    curation = world.state()["curation"]
    for stop in queue:
        curation = accept(curation, stop)
    res = world.save(curation, "Assign the review queue", base=base)
    assert res.status_code == 200, res.text
    assert res.json()["validation"]["errors"] == []

    after = review(world)
    assert after["version"] == res.json()["commit"]
    assert after["unassigned"] == []
    stations = state_stations(world)
    new_id = next(s["proposal"]["newStation"]["id"] for s in queue if s["platform"] == "SF:99200")
    assert [p["id"] for p in stations[new_id]["platforms"]] == ["SF:99200", "SF:99201"]
    assert "SF:99100" in [p["id"] for p in stations["churchMarket"]["platforms"]]
    assert stations["churchMarket"]["verified"] is None
    # The public network has them too.
    assert world.app.state.network.station_of("SF:99100") == "churchMarket"


def test_ignoring_takes_a_stop_out_of_the_queue(world):
    curation = copy.deepcopy(world.state()["curation"])
    curation["ignored"]["SF:15418"] = {"note": "A mezzanine pole no vehicle stops at."}
    res = world.save(curation, "Ignore the mezzanine pole")
    assert res.status_code == 200, res.text
    assert review(world)["unassigned"] == []
