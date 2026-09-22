"""``/map``: the public, read-only copy of the editor."""

from editor_world import commit_file, git


def test_map_state_needs_no_session(world):
    res = world.client.get("/map/api/state")
    assert res.status_code == 200, res.text
    state = res.json()
    assert state["version"] == world.seed
    assert (state["readOnly"], state["readOnlyReason"]) == (True, "public map")
    assert state["validation"] is None
    assert state["repo"] is None
    assert "castroPlaza" in state["curation"]["stations"]["stations"]
    assert set(state["snapshots"]) == {"SF"}
    assert state["derived"]["stations"]["castroPlaza"]["lat"] is not None
    assert res.headers["etag"] == f'"{world.seed}"'
    assert res.headers["cache-control"] == "no-cache"


def test_map_state_honours_if_none_match(world):
    etag = world.client.get("/map/api/state").headers["etag"]
    res = world.client.get("/map/api/state", headers={"If-None-Match": etag})
    assert res.status_code == 304
    assert res.content == b""
    assert res.headers["etag"] == etag

    # A new version is a full response again.
    world.login()
    curation = world.state()["curation"]
    curation["stations"]["stations"]["castroPlaza"]["name"] = "Castro Square"
    commit = world.save(curation).json()["commit"]
    res = world.client.get("/map/api/state", headers={"If-None-Match": etag})
    assert res.status_code == 200
    assert res.headers["etag"] == f'"{commit}"'
    assert res.json()["curation"]["stations"]["stations"]["castroPlaza"]["name"] == "Castro Square"


def test_map_page_is_public(world):
    res = world.client.get("/map/", follow_redirects=False)
    assert res.status_code == 200
    assert "<html" in res.text
    assert world.client.get("/map", follow_redirects=False).headers["location"] == "/map/"


def test_map_state_is_never_editable(make):
    # Even with no password, where /editor itself is open, /map says "public map".
    world = make(editor_password="")
    assert world.client.get("/map/api/state").json()["readOnlyReason"] == "public map"


def test_state_follows_a_checkout_the_network_has_not_caught_up_with(world):
    # Something moved the checkout without swapping the network: the state is
    # still self-consistent, labelled and derived from the checkout's own HEAD.
    theirs = commit_file(world.other, "curation/ignored.json", "{}\n", "Unignore everything")
    git(world.checkout, "pull", "-q", "--ff-only")
    state = world.client.get("/map/api/state").json()
    assert state["version"] == theirs
    assert state["curation"]["ignored"] == {}
    assert world.app.state.network.version == world.seed
