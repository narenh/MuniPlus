"""The assembled app: startup order, and degrading instead of failing."""

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings

FIXTURE = Path(__file__).parent / "fixtures" / "transit"


@pytest.fixture
def remote(tmp_path) -> Path:
    """A bare repo holding the sample dataset, standing in for GitHub's sf-transit."""
    seed = tmp_path / "seed"
    shutil.copytree(FIXTURE, seed)
    git = ["git", "-C", str(seed), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "seed"], check=True)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True)
    return bare


def settings(tmp_path: Path, repo: Path, **overrides) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path / "data", data_repo=str(repo), data_branch="main", **overrides)


def test_starts_clones_loads_and_polls_fixtures(tmp_path, remote):
    with TestClient(create_app(settings(tmp_path, remote, fixtures=True))) as client:
        health = client.get("/health").json()
        assert health["ok"], health["problems"]
        assert set(health["feeds"]) == {"SF:tripupdates", "SF:vehiclepositions", "SF:servicealerts"}

        stations = client.get("/api/v1/stations", headers={"Accept-Encoding": "gzip"})
        assert stations.status_code == 200
        assert stations.json()["version"] == health["version"]
        assert client.get("/api/v1/arrivals", params={"platforms": "SF:16992"}).status_code == 200
    assert (tmp_path / "data" / "sf-transit" / ".git").is_dir()


def test_without_realtime_the_data_still_serves(tmp_path, remote):
    with TestClient(create_app(settings(tmp_path, remote))) as client:
        assert client.get("/api/v1/stations/embarcadero").status_code == 200
        health = client.get("/health").json()
        assert not health["ok"]
        assert health["problems"] == ["realtime is off: no API_511_KEY and not FIXTURES"]


def test_with_no_data_the_server_still_boots_and_says_why(tmp_path):
    with TestClient(create_app(settings(tmp_path, tmp_path / "missing.git", fixtures=True))) as client:
        response = client.get("/api/v1/stations")
        assert response.status_code == 503
        assert response.json()["error"] == "unavailable"
        problems = client.get("/health").json()["problems"]
        assert any(p.startswith("no station data") for p in problems)


def test_large_responses_are_compressed(tmp_path, remote):
    with TestClient(create_app(settings(tmp_path, remote))) as client:
        response = client.get("/api/v1/lines", headers={"Accept-Encoding": "gzip"})
        assert response.headers.get("content-encoding") == "gzip"
