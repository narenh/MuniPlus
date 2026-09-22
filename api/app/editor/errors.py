"""Errors as ``Problem`` bodies, and the route class every editor router uses.

FastAPI's own errors come out as ``{"detail": ...}``, and a dependency cannot
return a response, only raise. So the editor's routers use ``ProblemRoute``: it
runs the router's checks (session, content type) before FastAPI even reads the
body, and turns a ``ProblemError`` or a malformed body into a ``Problem``. It is
set per router rather than as an app-wide exception handler, because ``app.main``
is the integrator's and the public endpoints already shape their own errors.
"""

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel

from ..models.api import Problem

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class ProblemError(Exception):
    def __init__(
        self,
        status: int,
        error: str,
        message: str,
        *,
        body: Problem | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.body = body or Problem(error=error, message=message)
        self.headers = headers

    def response(self) -> JSONResponse:
        return JSONResponse(status_code=self.status, content=self.body.model_dump(mode="json"), headers=self.headers)


def json_response(model: BaseModel, status: int = 200) -> Response:
    """Serialise once. The editor's payloads are large (every snapshot stop), and
    returning the model would have FastAPI validate it all over again."""
    return Response(content=model.model_dump_json(), status_code=status, media_type="application/json")


def require_json(request: Request) -> None:
    """Refuse a state-changing request that is not ``application/json``.

    This, with the session cookie's ``SameSite=Strict``, is the CSRF defence. A
    page on another site can make a browser send a cross-site request without any
    CORS check only with a "simple" content type (form-urlencoded, multipart,
    text/plain), which a plain HTML form can do. ``application/json`` is not simple:
    a cross-origin fetch sending it needs a CORS preflight, which this server never
    answers (it has no CORS middleware), so the request is never sent. ``Strict``
    alone would already keep the cookie off cross-site requests, but "site" is the
    registrable domain, so another ``*.canopysf.com`` host counts as same-site; the
    content type still stops that one, because it is cross-origin.
    """
    if request.method not in UNSAFE_METHODS:
        return
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise ProblemError(415, "bad-request", "Send the body as application/json.")


def _describe(err: RequestValidationError) -> str:
    # A few locations is enough to find a mistake; the full list for a broken
    # 3,000-station document would be longer than the document.
    parts = []
    for e in err.errors()[:5]:
        where = ".".join(str(p) for p in e.get("loc", ()) if p != "body")
        parts.append(f"{where}: {e.get('msg')}" if where else str(e.get("msg")))
    more = len(err.errors()) - len(parts)
    return "Malformed request: " + "; ".join(parts) + (f" (and {more} more)" if more > 0 else "")


class ProblemRoute(APIRoute):
    """Checks run before the handler, and errors come out as ``Problem``s."""

    def check(self, request: Request) -> None:
        require_json(request)

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def run(request: Request) -> Response:
            try:
                self.check(request)
                return await handler(request)
            except ProblemError as err:
                return err.response()
            except RequestValidationError as err:
                return ProblemError(400, "bad-request", _describe(err)).response()

        return run
