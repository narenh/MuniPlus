"""HTTP caching shared by the cached endpoints (``/map/api/state``, the shapes,
the editor page), and the loaded network.

``request.app.state.network`` is replaced whole on a reload, so a handler takes
it once and uses that one value throughout.
"""

import functools
import hashlib
import json

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from ..data.network import Network
from ..models.api import Problem

# The ETag is the data repo's commit plus a hash of the response schemas. The
# commit alone said nothing about shape: after a deploy that changed a response
# without changing the data, a client revalidating would get a 304 and keep an
# old-shaped body (URLSession's default cache does exactly this). It is quoted,
# as RFC 9110 requires; ``no-cache`` makes clients revalidate every time instead
# of guessing a freshness lifetime, since a data edit can land at any moment.
CACHE_CONTROL = "no-cache"


@functools.cache
def schema_hash() -> str:
    """Of every response a cached endpoint sends, so any change to any of them
    moves every ETag. Computed once: the schema cannot change while running."""
    from ..models.api import ShapesResponse
    from ..models.editor import EditorState

    models = (ShapesResponse, EditorState)
    text = json.dumps([m.model_json_schema(by_alias=True) for m in models], sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def _tag(version: str) -> str:
    return f"{version}.{schema_hash()}"


def etag_of(version: str) -> str:
    return f'"{_tag(version)}"'


def not_modified(request: Request, version: str) -> bool:
    """Whether ``If-None-Match`` already names this version of this schema. Lenient
    about ``W/`` (Cloudflare weakens every ETag) and missing quotes, since for a
    304 a false negative only costs a body."""
    header = request.headers.get("if-none-match")
    if not header:
        return False
    current = _tag(version)
    for tag in header.split(","):
        tag = tag.strip().removeprefix("W/").strip('"')
        if tag == "*" or tag == current:
            return True
    return False


def cached(request: Request, response: Response, version: str) -> Response | None:
    """A 304 if the client is current; otherwise None, with the caching headers set
    on ``response`` for the 200."""
    headers = {"ETag": etag_of(version), "Cache-Control": CACHE_CONTROL}
    if not_modified(request, version):
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return None


def not_found(message: str) -> JSONResponse:
    return JSONResponse(status_code=404, content=Problem(error="not-found", message=message).model_dump())


class DataUnavailable(Exception):
    """No network is loaded: the data checkout could not be cloned or read at
    startup. ``transit_editor.main`` turns it into a 503; ``/health`` says why."""


def network(request: Request) -> Network:
    net = getattr(request.app.state, "network", None)
    if net is None:
        raise DataUnavailable
    return net
