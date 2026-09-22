"""What the editor server keeps on ``app.state.editor``, and ``EditorState``.

``EditorState`` is built for both pages. The editor gets validation and repo
status; ``/map`` gets neither, and is always read-only.
"""

import time
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, Request

from ..data.network import Network
from ..data.repo import RepoError
from ..models.editor import EditorState
from ..settings import Settings
from .auth import NO_PASSWORD, Sessions, Throttle
from .checkout import Checkout, Loaded
from .errors import ProblemError

EDITOR_DIR = Path(__file__).resolve().parents[2] / "editor"
"""``api/editor/``: the frontend, served at both ``/editor/`` and ``/map/``."""

PUBLIC_MAP = "public map"


class Editor:
    def __init__(
        self,
        settings: Settings,
        *,
        checkout: Checkout | None = None,
        editor_dir: Path = EDITOR_DIR,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings
        self.checkout = checkout or Checkout(settings)
        self.sessions = Sessions(settings)
        self.throttle = Throttle(clock)
        self.editor_dir = editor_dir

    def read_only_reason(self) -> str | None:
        if not self.settings.editor_password:
            return NO_PASSWORD
        return self.checkout.cannot_write()


def editor_of(request: Request) -> Editor:
    return request.app.state.editor


def require_checkout(editor: Editor) -> None:
    if not editor.checkout.exists:
        raise ProblemError(503, "unavailable", "There is no sf-transit checkout on this server.")


def current_network(app: FastAPI) -> Network | None:
    return getattr(app.state, "network", None)


def loaded(editor: Editor) -> Loaded:
    """The checkout's files at HEAD, or a 503 saying why they cannot be had."""
    require_checkout(editor)
    try:
        return editor.checkout.loaded()
    except (RepoError, ValueError) as err:
        # ValueError covers what ``files`` refuses: bad JSON, a duplicate key, a
        # file that fails its model. Someone pushed it straight to sf-transit; the
        # editor cannot load it, so it cannot fix it either, and says so.
        raise ProblemError(503, "unavailable", f"Could not read the sf-transit checkout: {err}") from None


def build_state(editor: Editor, app: FastAPI, *, public: bool) -> EditorState:
    files_at_head = loaded(editor)
    try:
        repo = None if public else editor.checkout.status()
    except RepoError as err:
        raise ProblemError(503, "unavailable", f"Could not read the sf-transit checkout: {err}") from None
    # The app's network is the one the public API serves; reuse it when it is this
    # version, which is always, except for the moment between a save moving HEAD
    # and the swap, or if something else moved the checkout without a reload.
    network = current_network(app)
    if network is None or network.version != files_at_head.version:
        network = files_at_head.network()
    reason = PUBLIC_MAP if public else editor.read_only_reason()
    return EditorState(
        version=files_at_head.version,
        read_only=reason is not None,
        read_only_reason=reason,
        curation=files_at_head.curation,
        snapshots=files_at_head.snapshots,
        derived=network.derived(),
        validation=None if public else files_at_head.validation(),
        repo=repo,
    )
