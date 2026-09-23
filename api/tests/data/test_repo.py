"""The sf-transit checkout, against a local bare repo standing in for GitHub."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from app.data import loader
from app.data.repo import Repo, RepoError
from app.settings import Settings

FIXTURE = Path(__file__).parent.parent / "fixtures" / "transit"

# Tests commit with a fixed identity and none of the machine's own git config
# (signing, hooks), so they behave the same on any laptop or CI box.
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch):
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit_file(work: Path, rel: str, text: str, message: str) -> str:
    (work / rel).parent.mkdir(parents=True, exist_ok=True)
    (work / rel).write_text(text)
    git(work, "add", rel)
    git(work, "commit", "-q", "-m", message)
    git(work, "push", "-q", "origin", "main")
    return git(work, "rev-parse", "HEAD")


@pytest.fixture
def origin(tmp_path) -> tuple[Path, Path]:
    """A bare "GitHub" holding the fixture, and a second working copy that plays
    another editor pushing to it."""
    bare = tmp_path / "sf-transit.git"
    git(tmp_path, "init", "-q", "--bare", "--initial-branch=main", str(bare))
    other = tmp_path / "other"
    shutil.copytree(FIXTURE, other)
    git(other, "init", "-q", "--initial-branch=main")
    git(other, "remote", "add", "origin", str(bare))
    git(other, "add", ".")
    git(other, "commit", "-q", "-m", "Seed")
    git(other, "push", "-q", "origin", "main")
    return bare, other


def settings_for(tmp_path: Path, bare: Path, **extra) -> Settings:
    # ``_env_file=None``: never read api/.env from a test. And no key from the
    # shell's own environment unless a test sets one.
    extra.setdefault("data_deploy_key", "")
    return Settings(_env_file=None, data_dir=tmp_path / "data", data_repo=str(bare), data_branch="main", **extra)


def test_clone_when_absent(tmp_path, origin):
    bare, other = origin
    repo = Repo(settings_for(tmp_path, bare))
    assert repo.ensure_cloned() is True
    assert repo.path == tmp_path / "data" / "sf-transit"
    assert repo.head() == git(other, "rev-parse", "HEAD")
    assert repo.ensure_cloned() is False


def test_network_from_checkout(tmp_path, origin):
    bare, other = origin
    repo = Repo(settings_for(tmp_path, bare))
    repo.ensure_cloned()
    net = loader.load_checkout(repo, ["SF"])
    assert net.version == git(other, "rev-parse", "HEAD")
    assert net.stations().former_ids == {"mongomery": "montgomery"}


def test_refuses_a_non_empty_non_checkout(tmp_path, origin):
    bare, _ = origin
    repo = Repo(settings_for(tmp_path, bare))
    repo.path.mkdir(parents=True)
    (repo.path / "stray.txt").write_text("x")
    with pytest.raises(RepoError, match="not a git checkout"):
        repo.ensure_cloned()


def test_clone_of_a_missing_branch_fails(tmp_path, origin):
    bare, _ = origin
    repo = Repo(settings_for(tmp_path, bare).model_copy(update={"data_branch": "nope"}))
    with pytest.raises(RepoError, match="clone failed"):
        repo.ensure_cloned()


def test_pull(tmp_path, origin):
    bare, other = origin
    repo = Repo(settings_for(tmp_path, bare))
    repo.ensure_cloned()
    new = commit_file(other, "curation/ignored.json", '{}\n', "Unignore everything")
    assert repo.head() != new
    repo.pull()
    assert repo.head() == new
    assert (repo.path / "curation/ignored.json").read_text() == "{}\n"


def test_pull_refuses_to_merge(tmp_path, origin):
    bare, other = origin
    repo = Repo(settings_for(tmp_path, bare))
    repo.ensure_cloned()
    commit_file(other, "a.txt", "theirs\n", "Theirs")
    (repo.path / "b.txt").write_text("ours\n")
    git(repo.path, "add", "b.txt")
    git(repo.path, "commit", "-q", "-m", "Ours")
    before = repo.head()
    with pytest.raises(RepoError, match="pull failed"):
        repo.pull()
    assert repo.head() == before
    assert not repo.status().dirty


def test_status(tmp_path, origin):
    bare, other = origin
    repo = Repo(settings_for(tmp_path, bare))
    repo.ensure_cloned()
    status = repo.status()
    assert (status.branch, status.head, status.dirty, status.ahead, status.behind) == (
        "main", repo.head(), False, 0, 0,
    )  # fmt: skip

    (repo.path / "curation/ignored.json").write_text("{}\n")
    assert repo.status().dirty

    git(repo.path, "commit", "-q", "-am", "Local edit")
    assert (repo.status().ahead, repo.status().behind, repo.status().dirty) == (1, 0, False)

    # Behind counts from the last fetch; status itself never touches the network.
    commit_file(other, "a.txt", "x\n", "Remote edit")
    assert repo.status().behind == 0
    git(repo.path, "fetch", "-q")
    assert (repo.status().ahead, repo.status().behind) == (1, 1)


# MARK: - Deploy key


KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nnot-a-real-key\n-----END OPENSSH PRIVATE KEY-----"


def test_deploy_key_is_written_0600_for_the_command_only(tmp_path, origin):
    bare, _ = origin
    repo = Repo(settings_for(tmp_path, bare, data_deploy_key=KEY))
    with repo._remote_env() as env:
        command = env["GIT_SSH_COMMAND"]
        key_file = Path(command.split()[2])
        assert command == f"ssh -i {key_file} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
        assert key_file.read_text() == KEY + "\n"
    assert not key_file.exists()


def test_no_key_no_ssh_command(tmp_path, origin):
    bare, _ = origin
    with Repo(settings_for(tmp_path, bare))._remote_env() as env:
        # Whatever the machine had, untouched: without a key, ssh is not our business.
        assert env.get("GIT_SSH_COMMAND") == os.environ.get("GIT_SSH_COMMAND")
        assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_clone_and_pull_with_a_key_set(tmp_path, origin):
    # A local path never uses ssh, so this checks the key plumbing does not get in
    # the way, and that an error never carries the key.
    bare, _ = origin
    repo = Repo(settings_for(tmp_path, bare, data_deploy_key=KEY))
    repo.ensure_cloned()
    repo.pull()
    broken = Repo(settings_for(tmp_path / "b", bare, data_deploy_key=KEY).model_copy(update={"data_branch": "nope"}))
    with pytest.raises(RepoError) as failure:
        broken.ensure_cloned()
    assert "not-a-real-key" not in str(failure.value)


def test_deploy_keys_survive_env_var_mangling():
    # The first staging deploy failed with "error in libcrypto": the key's newlines
    # did not survive the trip through the env var.
    import subprocess, tempfile
    from pathlib import Path
    from app.data.repo import normalise_key

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "k"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True)
        original = path.read_text()
        for mangled in (original, original.strip(), original.replace("\n", "\\n"), original.replace("\n", " ").strip()):
            fixed = normalise_key(mangled)
            path.write_text(fixed)
            path.chmod(0o600)
            # ssh-keygen -y only succeeds on a key file ssh can actually load.
            assert subprocess.run(["ssh-keygen", "-y", "-f", str(path)], capture_output=True).returncode == 0, repr(mangled[:60])
