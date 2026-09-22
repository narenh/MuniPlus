"""``GET /map/api/state``: the editor's state for the public, read-only map.

No login. The same ``EditorState`` the editor loads, with ``readOnly`` set and no
validation or repo status; the frontend at ``/map/`` hides everything that edits
because of ``readOnly``. Nothing in it is private: sf-transit is a public repo.
"""

from fastapi import APIRouter, Request, Response

from ..data.repo import RepoError
from ..editor.errors import ProblemError, ProblemRoute, json_response
from ..editor.state import build_state, editor_of, require_checkout
from ..models.api import Problem
from ..models.editor import EditorState
from .stations import CACHE_CONTROL, etag_of, not_modified

router = APIRouter(prefix="/map/api", route_class=ProblemRoute, tags=["map"])


@router.get("/state", response_model=EditorState, responses={503: {"model": Problem}})
def map_state(request: Request):
    # The body carries every snapshot stop and pattern, so a revisit at the same
    # version should cost a 304, not the whole payload. The HEAD is one cheap git
    # call; the state is only built when the client does not have it.
    editor = editor_of(request)
    require_checkout(editor)
    try:
        version = editor.checkout.head()
    except RepoError as err:
        raise ProblemError(503, "unavailable", f"Could not read the sf-transit checkout: {err}") from None
    if not_modified(request, version):
        return Response(status_code=304, headers={"ETag": etag_of(version), "Cache-Control": CACHE_CONTROL})
    state = build_state(editor, request.app, public=True)
    response = json_response(state)
    # From the body's own version, in case a save moved HEAD in between.
    response.headers.update({"ETag": etag_of(state.version), "Cache-Control": CACHE_CONTROL})
    return response
