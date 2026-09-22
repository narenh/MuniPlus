"""Helpers for the editor tests: a local bare repo standing in for GitHub, seeded
with the fixture network, a second working copy that plays another editor, and
the app wired up the way ``app.main`` will do it.

A module rather than only fixtures so the tests can call ``git`` and friends.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.data import loader
from app.data.repo import Repo
from app.editor.install import install_editor
from app.settings import Settings

FIXTURE = Path(__file__).parent.parent / "fixtures" / "transit"
PASSWORD = "correct horse battery staple"
SECRET = "test-secret-" + "x" * 32

# Tests commit with a fixed identity and none of the machine's own git config
# (signing, hooks), so they behave the same on any laptop or CI box. The editor's
# own commits must not depend on these: it sets its author explicitly.
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Other",
    "GIT_AUTHOR_EMAIL": "other@example.com",
    "GIT_COMMITTER_NAME": "Other",
    "GIT_COMMITTER_EMAIL": "other@example.com",
}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit_file(work: Path, rel: str, text: str, message: str) -> str:
    """Commit in the other working copy and push it, as another editor would."""
    git(work, "pull", "-q", "--ff-only", "origin", "main")
    (work / rel).parent.mkdir(parents=True, exist_ok=True)
    (work / rel).write_text(text)
    git(work, "add", rel)
    git(work, "commit", "-q", "-m", message)
    git(work, "push", "-q", "origin", "main")
    return git(work, "rev-parse", "HEAD")


def edit_file(work: Path, rel: str, old: str, new: str, message: str) -> str:
    git(work, "pull", "-q", "--ff-only", "origin", "main")
    text = (work / rel).read_text()
    assert text.count(old) == 1, f"{old!r} is not unique in {rel}"
    return commit_file(work, rel, text.replace(old, new), message)


@dataclass
class World:
    app: FastAPI
    client: TestClient
    settings: Settings
    bare: Path
    other: Path
    checkout: Path
    seed: str
    password: str = PASSWORD

    def login(self) -> None:
        res = self.client.post("/editor/login", json={"password": PASSWORD})
        assert res.status_code == 204, res.text

    def state(self) -> dict:
        res = self.client.get("/editor/api/state")
        assert res.status_code == 200, res.text
        return res.json()

    def save(self, curation: dict, message: str = "Edit", base: str | None = None):
        return self.client.post(
            "/editor/api/save",
            json={"curation": curation, "message": message, "baseVersion": base or self.seed},
        )

    def head(self) -> str:
        return git(self.checkout, "rev-parse", "HEAD")

    def remote_head(self) -> str:
        return git(self.bare, "rev-parse", "main")


def make_origin(tmp_path: Path) -> tuple[Path, Path]:
    bare = tmp_path / "sf-transit.git"
    git(tmp_path, "init", "-q", "--bare", "--initial-branch=main", str(bare))
    other = tmp_path / "other"
    shutil.copytree(FIXTURE, other)
    git(other, "init", "-q", "--initial-branch=main")
    git(other, "remote", "add", "origin", str(bare))
    git(other, "add", ".")
    git(other, "commit", "-q", "-m", "Seed")
    git(other, "push", "-q", "-u", "origin", "main")
    return bare, other


def make_world(tmp_path: Path, origin: tuple[Path, Path], **overrides) -> World:
    bare, other = origin
    values = dict(
        data_dir=tmp_path / "data",
        data_repo=str(bare),
        data_branch="main",
        data_deploy_key="",
        editor_password=PASSWORD,
        session_secret=SECRET,
        git_author_name="Editor",
        git_author_email="editor@example.com",
    )
    values.update(overrides)
    # ``_env_file=None``: never read api/.env from a test.
    settings = Settings(_env_file=None, **values)
    repo = Repo(settings)
    repo.ensure_cloned()
    # A bare app with only this track's routes: ``app.main`` is the integrator's.
    app = FastAPI()
    app.state.settings = settings
    app.state.network = loader.load_checkout(repo, settings.operators)
    install_editor(app, settings)
    # https, or the client would (rightly) never send back a Secure cookie.
    client = TestClient(app, base_url="https://testserver")
    return World(app, client, settings, bare, other, repo.path, repo.head())

