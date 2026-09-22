"""The sf-transit checkout: the read side of git.

The server keeps one clone at ``settings.transit_dir`` and reads the network from
it. This module clones it, pulls it and reports on it; committing and pushing
editor saves is added by the editor server (track F) on top of ``_git``.

It shells out to the git CLI rather than use a library, because the one thing
this code must get right is authenticating with a deploy key over ssh, and git
already does that the way everyone debugs it.

The deploy key arrives as an env var (``DATA_DEPLOY_KEY``), not a file. It is
written to a 0600 temp file only for the length of one command that talks to
the remote, and removed straight after. It is never logged: it goes to ssh through
a file path in ``GIT_SSH_COMMAND``, never on a command line, and errors carry only
git's own stderr.
"""

import os
import shlex
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..models.editor import RepoStatus
from ..settings import Settings

TIMEOUT_SECONDS = 120
"""sf-transit is small JSON, so a command still running after two minutes is stuck
(usually the network), and a request waiting on a pull should fail rather than
hang the one worker."""


class RepoError(RuntimeError):
    """A git command failed. The message is git's stderr, which never holds the key."""


class Repo:
    def __init__(self, settings: Settings):
        self.path: Path = settings.transit_dir
        self.url = settings.data_repo
        self.branch = settings.data_branch
        self._deploy_key = settings.data_deploy_key

    # MARK: Reading

    def ensure_cloned(self) -> bool:
        """Clone the data repo at the configured branch if there is no checkout yet.
        Returns whether it cloned."""
        if (self.path / ".git").exists():
            return False
        if self.path.exists() and any(self.path.iterdir()):
            # Cloning into it would fail anyway; say why instead of git's generic error.
            raise RepoError(f"{self.path} exists, is not empty and is not a git checkout")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._remote_env() as env:
            self._run(
                ["git", "clone", "--branch", self.branch, "--single-branch", self.url, str(self.path)],
                env=env,
                cwd=self.path.parent,
            )
        return True

    def pull(self) -> None:
        """Fast-forward to the remote branch.

        ``--ff-only``, because the read side must never create a merge or stop
        half-way through a rebase: if the checkout has diverged (an editor commit
        that failed to push), this fails and the checkout is left as it was for the
        save path to reconcile, which has the context to do it.
        """
        with self._remote_env() as env:
            self._git("pull", "--ff-only", "origin", self.branch, env=env)

    def head(self) -> str:
        """The checked-out commit. This is the network's ``version``."""
        return self._git("rev-parse", "HEAD")

    def status(self) -> RepoStatus:
        """Where the checkout stands. Ahead/behind are against the last fetch: this
        does not touch the network, so it is cheap enough to call per request."""
        branch = self._git("rev-parse", "--abbrev-ref", "HEAD")
        dirty = bool(self._git("status", "--porcelain"))
        try:
            counts = self._git("rev-list", "--left-right", "--count", "HEAD...@{upstream}")
            ahead, behind = (int(n) for n in counts.split())
        except RepoError:
            ahead = behind = 0  # no upstream (a detached HEAD, or a local-only branch)
        return RepoStatus(branch=branch, head=self.head(), dirty=dirty, ahead=ahead, behind=behind)

    # MARK: Running git

    def _git(self, *args: str, env: dict[str, str] | None = None) -> str:
        return self._run(["git", *args], env=env, cwd=self.path)

    def _run(self, cmd: list[str], *, env: dict[str, str] | None, cwd: Path) -> str:
        try:
            done = subprocess.run(
                cmd,
                cwd=cwd,
                env=env if env is not None else self._base_env(),
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise RepoError(f"git {cmd[1]} timed out after {TIMEOUT_SECONDS}s") from None
        if done.returncode != 0:
            raise RepoError(f"git {cmd[1]} failed: {done.stderr.strip()}")
        return done.stdout.strip()

    @staticmethod
    def _base_env() -> dict[str, str]:
        env = dict(os.environ)
        # Never wait on a prompt for a password or passphrase: there is nobody to type it.
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    @contextmanager
    def _remote_env(self) -> Iterator[dict[str, str]]:
        """The environment for a command that talks to the remote, with the deploy
        key on disk for exactly as long as the command runs."""
        env = self._base_env()
        if not self._deploy_key:
            yield env
            return
        fd, key_path = tempfile.mkstemp(prefix="sf-transit-key-")
        try:
            # mkstemp already creates the file 0600; the chmod states the requirement,
            # since ssh refuses a key others can read.
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                # Some OpenSSH versions reject a key file without its trailing
                # newline ("invalid format"), and an env var loses it easily.
                f.write(self._deploy_key if self._deploy_key.endswith("\n") else self._deploy_key + "\n")
            env["GIT_SSH_COMMAND"] = (
                f"ssh -i {shlex.quote(key_path)} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
            )
            yield env
        finally:
            Path(key_path).unlink(missing_ok=True)
