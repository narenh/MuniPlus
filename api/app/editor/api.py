"""``/editor/api/*``: state, validate, save, history. All need the session.

The one exception: with no editor password configured, nobody can have a
session, yet ``/editor`` still serves the editor read-only (PLAN.md), and that
page loads ``api/state``. So then the read-only ``GET`` endpoints are open, as
``/map/api/state`` always is; everything they return is in the public data repo
already. ``POST``s stay closed.
"""

import re

from fastapi import APIRouter, Request

from ..data.network import Network
from ..data.repo import RepoError
from ..data.validate import validate
from ..models.api import Problem
from ..models.editor import EditorState, SaveRequest, SaveResponse, ValidateRequest, ValidateResponse
from .errors import ProblemError, ProblemRoute, json_response, require_json
from .models import HistoryEntry, HistoryResponse, InvalidSave, SaveConflict
from .save import save as run_save
from .state import build_state, editor_of, loaded, require_checkout

HISTORY_LIMIT = 40

GITHUB_REMOTE = re.compile(
    r"(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)([\w.-]+)/([\w.-]+?)(?:\.git)?/?"
)


class SessionRoute(ProblemRoute):
    def check(self, request: Request) -> None:
        editor = editor_of(request)
        open_read = request.method == "GET" and not editor.settings.editor_password
        if not open_read and not editor.sessions.of(request):
            raise ProblemError(401, "unauthorized", "Log in at /editor/login.")
        # After the session check, so a caller without one learns nothing else.
        require_json(request)


router = APIRouter(prefix="/editor/api", route_class=SessionRoute, tags=["editor"])

UNAUTHORIZED = {401: {"model": Problem}}


@router.get("/state", response_model=EditorState, responses=UNAUTHORIZED)
def state(request: Request):
    return json_response(build_state(editor_of(request), request.app, public=False))


@router.post("/validate", response_model=ValidateResponse, responses=UNAUTHORIZED)
def validate_unsaved(body: ValidateRequest, request: Request):
    at_head = loaded(editor_of(request))
    # Labelled with the checkout's version, which is what the edit started from;
    # nothing about this network is saved.
    network = Network(body.curation, at_head.snapshots, at_head.version)
    return json_response(
        ValidateResponse(validation=validate(body.curation, at_head.snapshots), derived=network.derived())
    )


@router.post(
    "/save",
    response_model=SaveResponse,
    responses={
        **UNAUTHORIZED,
        409: {"model": SaveConflict},
        422: {"model": InvalidSave},
        503: {"model": Problem},
    },
)
def save(body: SaveRequest, request: Request):
    return json_response(run_save(editor_of(request), request.app, body))


@router.get("/history", response_model=HistoryResponse, responses=UNAUTHORIZED)
def history(request: Request):
    editor = editor_of(request)
    require_checkout(editor)
    try:
        rows = editor.checkout.history(HISTORY_LIMIT)
    except RepoError as err:
        raise ProblemError(503, "unavailable", f"Could not read sf-transit's history: {err}") from None
    base = github_base(editor.settings.data_repo)
    return HistoryResponse(
        commits=[
            HistoryEntry(sha=sha, subject=subject, author=author, date=date, url=f"{base}/commit/{sha}" if base else None)
            for sha, subject, author, date in rows
        ]
    )


def github_base(remote: str) -> str | None:
    """``git@github.com:owner/repo.git`` -> ``https://github.com/owner/repo``."""
    m = GITHUB_REMOTE.fullmatch(remote.strip())
    return f"https://github.com/{m.group(1)}/{m.group(2)}" if m else None
