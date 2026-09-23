"""State, validate, save and history, against a local bare repo."""

import copy
from datetime import datetime

import pytest

from app.editor.api import github_base
from app.editor.checkout import Checkout

from editor_world import commit_file, edit_file, git


@pytest.fixture
def world(world):
    world.login()
    return world


def rename(curation: dict, station: str, name: str) -> dict:
    out = copy.deepcopy(curation)
    out["stations"]["stations"][station]["name"] = name
    return out


def diff_lines(repo, commit: str) -> list[str]:
    """The +/- lines of a commit, without the file headers."""
    out = git(repo, "show", "--format=", "--unified=0", commit)
    return [l for l in out.splitlines() if l[:1] in "+-" and not l.startswith(("+++", "---"))]


# MARK: - State


def test_state(world):
    state = world.state()
    assert state["version"] == world.seed
    assert (state["readOnly"], state["readOnlyReason"]) == (False, None)
    assert "castroPlaza" in state["curation"]["stations"]["stations"]
    assert state["curation"]["lines"]["SF:F"]["mode"] == "streetcar"
    assert set(state["snapshots"]) == {"SF"}
    assert state["snapshots"]["SF"]["meta"]["operator"] == "SF"
    assert set(state["derived"]) == {"stations", "platforms", "lines"}
    assert state["derived"]["stations"]["castroPlaza"]["lat"] is not None
    assert state["validation"]["errors"] == []
    assert {i["code"] for i in state["validation"]["warnings"]} <= {
        "platform-not-in-snapshot", "station-has-no-live-platforms", "unassigned-stop",
        "unknown-line-override", "unknown-line",
    }  # fmt: skip
    assert state["repo"] == {"branch": "main", "head": world.seed, "dirty": False, "ahead": 0, "behind": 0}


def test_state_is_read_only_without_a_git_author(make):
    world = make(git_author_email="")
    world.login()
    state = world.state()
    assert (state["readOnly"], state["readOnlyReason"]) == (True, "no git author is configured")
    res = world.save(rename(state["curation"], "castroPlaza", "Castro"))
    assert res.status_code == 503
    assert world.head() == world.seed


def test_an_ssh_remote_without_a_key_cannot_write(world):
    settings = world.settings.model_copy(update={"data_repo": "git@github.com:narenh/sf-transit.git"})
    assert Checkout(settings).cannot_write() == "no deploy key is configured"
    keyed = settings.model_copy(update={"data_deploy_key": "a key"})
    assert Checkout(keyed).cannot_write() is None


# MARK: - Validate


def test_validate_an_unsaved_curation_with_an_error(world):
    curation = copy.deepcopy(world.state()["curation"])
    stations = curation["stations"]["stations"]
    stolen = stations["castroPlaza"]["platforms"][0]
    stations["clayDrumm"]["platforms"].append(stolen)
    res = world.client.post("/editor/api/validate", json={"curation": curation})
    assert res.status_code == 200, res.text
    body = res.json()
    assert [i["code"] for i in body["validation"]["errors"]] == ["platform-in-two-stations"]
    # Derived for the unsaved curation, where the first claimant in id order
    # keeps a doubly-listed platform: Castro Plaza keeps it, Clay & Drumm is as was.
    before = world.state()["derived"]["stations"]
    assert body["derived"]["stations"]["castroPlaza"] == before["castroPlaza"]
    assert body["derived"]["stations"]["clayDrumm"] == before["clayDrumm"]
    # Nothing written.
    assert git(world.checkout, "status", "--porcelain") == ""
    assert world.head() == world.seed


def test_validate_rejects_a_malformed_curation(world):
    curation = copy.deepcopy(world.state()["curation"])
    curation["stations"]["stations"]["castroPlaza"]["platforms"][0]["heading"] = "upwards"
    res = world.client.post("/editor/api/validate", json={"curation": curation})
    assert res.status_code == 400
    assert res.json()["error"] == "bad-request"
    assert "heading" in res.json()["message"]


# MARK: - Save


def test_rename_is_a_one_line_commit(world):
    old_network = world.app.state.network
    state = world.state()
    res = world.save(rename(state["curation"], "castroPlaza", "Castro Square"), message="Rename Castro Plaza")
    assert res.status_code == 200, res.text
    body = res.json()
    commit = body["commit"]
    assert commit == body["version"] == world.head() == world.remote_head()
    assert (body["pushed"], body["pushError"]) == (True, None)
    assert body["validation"]["errors"] == []

    assert diff_lines(world.checkout, commit) == [
        '-      "name": "Castro Plaza",',
        '+      "name": "Castro Square",',
    ]
    assert git(world.checkout, "show", "--format=%an <%ae>|%cn|%s", "--no-patch", commit) == (
        "Editor <editor@example.com>|Editor|Rename Castro Plaza"
    )
    assert git(world.checkout, "rev-parse", f"{commit}^") == world.seed
    assert git(world.checkout, "status", "--porcelain") == ""

    # The public API's network is replaced whole, at the new version.
    network = world.app.state.network
    assert network is not old_network
    assert network.version == commit
    assert network.station("castroPlaza").name == "Castro Square"
    assert old_network.station("castroPlaza").name == "Castro Plaza"
    assert world.state()["version"] == commit


def test_a_shape_patch_is_saved_to_its_own_file_and_served(world):
    # The editor's test app has no public routes; the network is what /api/shapes serves.
    served = lambda: [list(p) for p in world.app.state.network.shapes()[0].shapes["SF:F1"]]  # noqa: E731
    before = served()
    curation = copy.deepcopy(world.state()["curation"])
    path = [[-122.434979, 37.762576], [-122.4331, 37.765], [-122.429214, 37.76725]]
    curation["shapes"] = {"f-corner": {"lines": ["SF:F"], "path": path, "note": "Checked on site."}}
    res = world.save(curation, message="Patch the F at Castro")
    assert res.status_code == 200, res.text
    commit = res.json()["commit"]
    assert git(world.checkout, "show", "--format=", "--name-only", commit).split() == ["curation/shapes.json"]
    assert served() == path != before
    assert world.state()["curation"]["shapes"]["f-corner"]["note"] == "Checked on site."


def test_an_unchanged_save_makes_no_commit(world):
    network = world.app.state.network
    res = world.save(world.state()["curation"])
    assert res.status_code == 200, res.text
    body = res.json()
    assert (body["commit"], body["version"], body["pushed"]) == (None, world.seed, True)
    assert world.head() == world.remote_head() == world.seed
    assert world.app.state.network is network


def test_a_save_with_errors_is_refused(world):
    curation = copy.deepcopy(world.state()["curation"])
    curation["stations"]["stations"]["castroPlaza"]["platforms"] = []
    res = world.save(curation)
    assert res.status_code == 422
    body = res.json()
    assert body["error"] == "bad-request"
    assert [i["code"] for i in body["validation"]["errors"]] == ["station-has-no-platforms"]
    assert world.head() == world.seed
    assert git(world.checkout, "status", "--porcelain") == ""


def test_a_malformed_base_version_never_reaches_git(world):
    res = world.save(world.state()["curation"], base="--upload-pack=touch /tmp/x")
    assert res.status_code == 400
    assert res.json()["error"] == "bad-request"


def test_conflict_when_curation_changed_since_the_base(world):
    curation = world.state()["curation"]
    theirs = commit_file(world.other, "curation/ignored.json", "{}\n", "Unignore everything")
    res = world.save(rename(curation, "castroPlaza", "Castro Square"))
    assert res.status_code == 409
    body = res.json()
    assert body["error"] == "conflict"
    assert body["paths"] == ["curation/ignored.json"]
    assert body["version"] == theirs
    # Nothing of ours written; the checkout fast-forwarded to theirs, and the
    # network followed it.
    assert world.head() == world.remote_head() == theirs
    assert git(world.checkout, "status", "--porcelain") == ""
    assert '"Castro Plaza"' in (world.checkout / "curation/stations.json").read_text()
    assert world.app.state.network.version == theirs


def test_a_snapshot_change_since_the_base_is_not_a_conflict(world):
    curation = world.state()["curation"]
    theirs = edit_file(
        world.other, "snapshot/SF/meta.json", "2026-09-22T21:48:36Z", "2026-09-23T09:00:00Z", "Refresh the snapshot"
    )
    res = world.save(rename(curation, "castroPlaza", "Castro Square"))
    assert res.status_code == 200, res.text
    commit = res.json()["commit"]
    assert git(world.checkout, "rev-parse", f"{commit}^") == theirs
    assert world.remote_head() == commit
    assert len(diff_lines(world.checkout, commit)) == 2


def test_an_unknown_base_version_is_a_conflict(world):
    res = world.save(world.state()["curation"], base="0" * 40)
    assert res.status_code == 409


# MARK: - Push races


def race(world, monkeypatch, then):
    """Make another editor push right after the save's first fetch, so the save's
    push is rejected."""
    checkout = world.app.state.editor.checkout
    real = checkout.fetch
    calls = []

    def fetch():
        real()
        calls.append(1)
        if len(calls) == 1:
            then()

    monkeypatch.setattr(checkout, "fetch", fetch)
    return calls


def test_rejected_push_is_rebased_and_retried(world, monkeypatch):
    curation = world.state()["curation"]
    theirs = []
    calls = race(
        world,
        monkeypatch,
        lambda: theirs.append(
            edit_file(world.other, "curation/lines.json", '"color": "#FAA633"', '"color": "#FAA634"', "Recolour J")
        ),
    )
    res = world.save(rename(curation, "castroPlaza", "Castro Square"))
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(calls) == 2  # the save's fetch, and the retry's
    assert body["pushed"] is True
    assert body["commit"] == world.head() == world.remote_head()
    assert git(world.checkout, "rev-parse", f"{body['commit']}^") == theirs[0]
    # Both edits are in, ours still one line.
    assert '"#FAA634"' in (world.checkout / "curation/lines.json").read_text()
    assert diff_lines(world.checkout, body["commit"]) == [
        '-      "name": "Castro Plaza",',
        '+      "name": "Castro Square",',
    ]
    assert world.app.state.network.version == body["commit"]


def test_rebase_conflict_restores_the_checkout(world, monkeypatch):
    curation = world.state()["curation"]
    theirs = []
    race(
        world,
        monkeypatch,
        lambda: theirs.append(
            edit_file(world.other, "curation/stations.json", '"Castro Plaza"', '"Castro Commons"', "Their rename")
        ),
    )
    network = world.app.state.network
    res = world.save(rename(curation, "castroPlaza", "Castro Square"))
    assert res.status_code == 409
    assert res.json()["error"] == "conflict"
    assert "at the same time" in res.json()["message"]  # the retry's conflict, not the base check
    # Back to where the save started: our commit gone, no rebase in progress,
    # nothing dirty. The remote has only theirs.
    assert world.head() == world.seed
    assert world.remote_head() == theirs[0]
    assert git(world.checkout, "status", "--porcelain") == ""
    assert not (world.checkout / ".git" / "rebase-merge").exists()
    assert not (world.checkout / ".git" / "rebase-apply").exists()
    assert '"Castro Plaza"' in (world.checkout / "curation/stations.json").read_text()
    assert world.app.state.network is network


def test_a_clean_rebase_that_breaks_validation_is_refused(world, monkeypatch):
    # Each side adds the same platform to a different station, far enough apart in
    # the file for git to merge them, and the result has it in two stations.
    curation = copy.deepcopy(world.state()["curation"])
    stray = {"id": "SF:99999", "heading": "northbound"}
    curation["stations"]["stations"]["montgomery"]["platforms"].append(stray)

    def theirs():
        text = (world.other / "curation/stations.json").read_text()
        before = '"id": "SF:13311",\n          "heading": "eastbound"\n        }'
        after = before + ',\n        {\n          "id": "SF:99999",\n          "heading": "northbound"\n        }'
        commit_file(world.other, "curation/stations.json", text.replace(before, after), "Theirs")

    race(world, monkeypatch, theirs)
    res = world.save(curation)
    assert res.status_code == 409, res.text
    assert "not together" in res.json()["message"]
    assert world.head() == world.seed
    assert git(world.checkout, "status", "--porcelain") == ""


def test_a_failed_push_keeps_the_commit_and_the_next_save_pushes_it(world):
    git(world.checkout, "remote", "set-url", "--push", "origin", str(world.bare.parent / "nowhere.git"))
    res = world.save(rename(world.state()["curation"], "castroPlaza", "Castro Square"))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["pushed"] is False
    assert body["pushError"]
    assert body["commit"] == world.head() != world.remote_head()
    assert world.app.state.network.version == body["commit"]
    assert world.state()["repo"]["ahead"] == 1

    # Fixed: an unchanged save makes no commit but pushes the one left behind.
    git(world.checkout, "remote", "set-url", "--push", "origin", str(world.bare))
    res = world.save(world.state()["curation"], base=body["commit"])
    assert res.status_code == 200, res.text
    assert (res.json()["commit"], res.json()["pushed"]) == (None, True)
    assert world.remote_head() == body["commit"]


# MARK: - History


def test_history(world):
    curation = world.state()["curation"]
    snapshot_only = edit_file(
        world.other, "snapshot/SF/meta.json", "2026-09-22T21:48:36Z", "2026-09-23T09:00:00Z", "Snapshot only"
    )
    commit = world.save(rename(curation, "castroPlaza", "Castro Square"), "Rename").json()["commit"]
    assert git(world.checkout, "rev-parse", f"{commit}^") == snapshot_only
    res = world.client.get("/editor/api/history")
    assert res.status_code == 200
    commits = res.json()["commits"]
    # Newest first, curation only: the snapshot commit is not there.
    assert [c["sha"] for c in commits] == [commit, world.seed]
    assert commits[0]["subject"] == "Rename"
    assert commits[0]["author"] == "Editor"
    assert datetime.fromisoformat(commits[0]["date"]).tzinfo is not None
    assert commits[0]["url"] is None  # a local path is not GitHub

    world.app.state.editor.settings = world.settings.model_copy(
        update={"data_repo": "git@github.com:narenh/sf-transit.git"}
    )
    url = world.client.get("/editor/api/history").json()["commits"][0]["url"]
    assert url == f"https://github.com/narenh/sf-transit/commit/{commit}"


@pytest.mark.parametrize(
    ("remote", "base"),
    [
        ("git@github.com:narenh/sf-transit.git", "https://github.com/narenh/sf-transit"),
        ("git@github.com:narenh/sf-transit", "https://github.com/narenh/sf-transit"),
        ("ssh://git@github.com/narenh/sf-transit.git", "https://github.com/narenh/sf-transit"),
        ("https://github.com/narenh/sf-transit.git", "https://github.com/narenh/sf-transit"),
        ("https://github.com/narenh/sf-transit", "https://github.com/narenh/sf-transit"),
        ("/tmp/sf-transit.git", None),
        ("git@gitlab.com:narenh/sf-transit.git", None),
    ],
)
def test_github_base(remote, base):
    assert github_base(remote) == base


# MARK: - Odds and ends


def test_a_checkout_that_does_not_load_is_a_503(world):
    # A hand edit pushed straight to sf-transit with a duplicate key.
    commit_file(world.other, "curation/ignored.json", '{"SF:1": {}, "SF:1": {}}\n', "Broken")
    git(world.checkout, "pull", "-q", "--ff-only")
    for res in (world.client.get("/editor/api/state"), world.client.get("/map/api/state")):
        assert res.status_code == 503
        assert res.json()["error"] == "unavailable"
        assert "duplicate key" in res.json()["message"]


def test_the_schema_still_builds(world):
    paths = world.app.openapi()["paths"]
    assert {"/editor/api/state", "/editor/api/save", "/editor/api/history", "/map/api/state"} <= set(paths)
