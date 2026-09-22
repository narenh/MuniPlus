"""The sf-transit checkout: the write side of git, and consistent reads of it.

Built on ``app.data.repo.Repo`` (clone, pull, status and the deploy-key
plumbing) by composition, running its git through ``Repo._git``, which that
module leaves for exactly this.

Two locks, because saves and reads have different needs:

* ``save_lock`` serialises saves end to end, network included. A save is a
  full-document write checked against ``baseVersion``, so two at once could each
  pass the check and the second would revert the first.
* ``lock`` is held only while the working tree changes (a fast-forward, a write
  and commit, a rebase, a reset) and while a reader takes the files and the HEAD
  they belong to. A reader never sees files a save has written but not yet
  committed, which would label new curation with the old version, and a slow
  push (up to ``repo.TIMEOUT_SECONDS``) never blocks ``/map`` or the editor's state.
"""

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ..data import loader
from ..data.network import Network
from ..data.repo import Repo, RepoError
from ..data.validate import validate
from ..models import files
from ..models.curation import Curation
from ..models.editor import RepoStatus, Validation
from ..models.snapshot import Snapshot
from ..settings import Settings

CURATION_DIR = "curation/"

SSH_REMOTE = re.compile(r"^(ssh://|[\w.-]+@[\w.-]+:)")
"""``git@github.com:owner/repo.git`` or ``ssh://...``: remotes that need a key.
A local path (the tests), ``file://`` or an https URL carrying its own token do not."""


class RebaseConflict(RuntimeError):
    """Replaying local commits onto the remote did not apply. Already aborted."""


@dataclass
class Loaded:
    """The files at one commit, read once and shared until HEAD moves. Nothing may
    mutate these models: every request reading this version gets the same ones."""

    version: str
    curation: Curation
    snapshots: dict[str, Snapshot]
    _network: Network | None = field(default=None, repr=False)
    _validation: Validation | None = field(default=None, repr=False)

    def network(self) -> Network:
        if self._network is None:
            self._network = Network(self.curation, self.snapshots, self.version)
        return self._network

    def validation(self) -> Validation:
        if self._validation is None:
            self._validation = validate(self.curation, self.snapshots)
        return self._validation


@dataclass
class PushResult:
    ok: bool
    rejected: bool = False
    """The remote has commits this checkout lacks: worth a fetch, rebase and retry."""
    error: str | None = None


class Checkout:
    def __init__(self, settings: Settings, repo: Repo | None = None):
        self.repo = repo or Repo(settings)
        self.operators = list(settings.operators)
        self.url = settings.data_repo
        self.upstream = f"origin/{settings.data_branch}"
        self._has_key = bool(settings.data_deploy_key)
        self._author = (settings.git_author_name, settings.git_author_email)
        self.save_lock = threading.Lock()
        # Re-entrant, so a save holding it can read through ``loaded`` as well.
        self.lock = threading.RLock()
        self._loaded: Loaded | None = None

    @property
    def path(self) -> Path:
        return self.repo.path

    @property
    def exists(self) -> bool:
        return (self.path / ".git").exists()

    def cannot_write(self) -> str | None:
        """Why a save could not succeed, judged from configuration alone.

        Deliberately no ``git push --dry-run``: that is a network round trip per
        state load, and a key that is set but wrong still shows up, as a
        ``pushError`` on the first save, with the commit kept to push later.
        """
        if not self.exists:
            return "there is no sf-transit checkout"
        if SSH_REMOTE.match(self.url) and not self._has_key:
            return "no deploy key is configured"
        if not all(self._author):
            # git refuses to commit without an identity, and the server has no
            # global config to fall back on.
            return "no git author is configured"
        return None

    # MARK: Reading

    def head(self) -> str:
        return self.repo.head()

    def status(self) -> RepoStatus:
        return self.repo.status()

    def loaded(self) -> Loaded:
        """The curation and snapshots at HEAD, read through ``app.models.files``."""
        with self.lock:
            head = self.repo.head()
            if self._loaded is None or self._loaded.version != head:
                curation, snapshots = loader.read(self.path, self.operators)
                self._loaded = Loaded(head, curation, snapshots)
            return self._loaded

    def history(self, limit: int) -> list[tuple[str, str, str, str]]:
        """(sha, subject, author, ISO date) of the last commits touching curation."""
        # Unit separators, because a subject can contain anything but a newline.
        out = self.repo._git("log", f"-n{limit}", "--format=%H%x1f%s%x1f%an%x1f%aI", "--", CURATION_DIR)
        return [tuple(line.split("\x1f", 3)) for line in out.splitlines() if line]

    def is_commit(self, rev: str) -> bool:
        try:
            self.repo._git("cat-file", "-e", f"{rev}^{{commit}}")
            return True
        except RepoError:
            return False

    def curation_changed(self, base: str, head: str) -> list[str]:
        out = self.repo._git("diff", "--name-only", base, head, "--", CURATION_DIR)
        return out.splitlines()

    def ahead(self) -> int:
        """Local commits the remote does not have, as of the last fetch."""
        return int(self.repo._git("rev-list", "--count", f"{self.upstream}..HEAD"))

    # MARK: Writing (callers hold ``save_lock``; tree changes also take ``lock``)

    def fetch(self) -> None:
        """Update ``origin/<branch>``. Touches no file, so it runs outside ``lock``."""
        with self.repo._remote_env() as env:
            self.repo._git("fetch", "-q", "origin", self.repo.branch, env=env)

    def rebase_onto_upstream(self) -> None:
        """Bring HEAD up to the last fetch.

        With nothing unpushed this is a fast-forward. With a commit left over from
        a save whose push failed, it replays that commit on top, so it is not lost
        and gets pushed with this save. If the replay does not apply, it is aborted
        and the checkout is exactly as before.
        """
        with self.lock:
            try:
                self.repo._git("rebase", "-q", self.upstream, env=self._author_env())
            except RepoError as err:
                try:
                    self.repo._git("rebase", "--abort")
                except RepoError:
                    pass  # it failed before starting (nothing to abort)
                raise RebaseConflict(str(err)) from None

    def commit_curation(self, curation: Curation, message: str) -> str | None:
        """Write the curation files and commit the ones that changed. Returns the
        new commit, or None when nothing changed (and nothing was committed)."""
        with self.lock:
            before = self.repo.head()
            changed = files.write_curation(self.path, curation)
            if not changed:
                return None
            try:
                self.repo._git("add", "--", *changed)
                # Only these paths: whatever else is in the index or the tree is
                # not this save's to commit.
                self.repo._git("commit", "-q", "--no-verify", "-m", message, "--", *changed, env=self._author_env())
            except BaseException:
                self.reset(before)
                raise
            return self.repo.head()

    def push(self) -> PushResult:
        with self.repo._remote_env() as env:
            try:
                self.repo._git("push", "-q", "origin", f"HEAD:refs/heads/{self.repo.branch}", env=env)
            except RepoError as err:
                # "! [rejected] ... (fetch first)" or "(non-fast-forward)": the remote
                # moved. "[remote rejected]" (a hook, a protected branch) and
                # everything else (no network, a bad key) is not fixed by a rebase.
                return PushResult(ok=False, rejected="[rejected]" in str(err), error=str(err))
        return PushResult(ok=True)

    def reset(self, rev: str) -> None:
        with self.lock:
            self.repo._git("reset", "-q", "--hard", rev)

    def _author_env(self) -> dict[str, str]:
        env = self.repo._base_env()
        name, email = self._author
        # Committer too: a rebase rewrites commits, and git refuses to without
        # a committer identity.
        env.update(
            GIT_AUTHOR_NAME=name,
            GIT_AUTHOR_EMAIL=email,
            GIT_COMMITTER_NAME=name,
            GIT_COMMITTER_EMAIL=email,
        )
        return env
