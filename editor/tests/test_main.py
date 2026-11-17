"""The assembled app: startup order, and degrading instead of failing."""

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from transit_editor.main import create_app
from transit_editor.settings import Settings

FIXTURE = Path(__file__).parent / "fixtures" / "transit"


@pytest.fixture
def remote(tmp_path) -> Path:
    """A bare repo holding the sample dataset, standing in for the data branch on GitHub."""
    seed = tmp_path / "seed"
    shutil.copytree(FIXTURE, seed)
    git = ["git", "-C", str(seed), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q", "-b", "static-data"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "seed"], check=True)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True)
    return bare


def settings(tmp_path: Path, repo: Path, **overrides) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path / "data", data_repo=str(repo), **overrides)


def test_defaults():
    s = Settings(_env_file=None)
    assert s.data_repo == "git@github.com:narenh/canopy-transit-editor.git"
    assert s.data_branch == "static-data"
    assert s.relay_url == "https://transit-relay.canopysf.com"
    assert s.transit_dir == Path("/data/data")


def test_starts_clones_and_loads(tmp_path, remote):
    with TestClient(create_app(settings(tmp_path, remote))) as client:
        health = client.get("/health").json()
        assert health["ok"], health["problems"]
        assert health["problems"] == []
        assert len(health["version"]) == 40

        state = client.get("/map/api/state", headers={"Accept-Encoding": "gzip"})
        assert state.status_code == 200
        assert state.headers.get("content-encoding") == "gzip"
        assert state.json()["version"] == health["version"]
        assert client.get("/map/").status_code == 200
        assert client.get("/", follow_redirects=False).headers["location"] == "/editor/"
    assert (tmp_path / "data" / "data" / ".git").is_dir()


def test_with_no_data_the_server_still_boots_and_says_why(tmp_path):
    with TestClient(create_app(settings(tmp_path, tmp_path / "missing.git"))) as client:
        assert client.get("/map/api/shapes").status_code == 503
        assert client.get("/map/api/state").status_code == 503
        health = client.get("/health").json()
        assert not health["ok"]
        assert any(p.startswith("no data") for p in health["problems"])


def test_no_app_facing_endpoints(tmp_path, remote):
    with TestClient(create_app(settings(tmp_path, remote))) as client:
        for path in ("/api/v1/stations", "/api/v1/lines", "/api/v1/arrivals", "/api/v1/vehicles",
                     "/api/v1/alerts", "/api/v1/speech", "/api/v1/shapes"):
            assert client.get(path).status_code == 404, path
