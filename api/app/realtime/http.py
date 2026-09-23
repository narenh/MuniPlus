"""Request parsing and responses shared by the three realtime endpoints.

Here rather than in ``app/endpoints/`` because that package is shared between
tracks and holds one module per resource.

Query parameters are taken as raw strings and validated here, not declared as
typed FastAPI parameters, because FastAPI answers a bad one with its own 422
body and every error from this API is a 400 ``Problem``.
"""

from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, TypeAdapter, ValidationError

from app.models.api import Problem
from app.models.ids import LineId, StopId, StationId

from .state import Realtime

NO_STORE = {"Cache-Control": "no-store"}
"""Every realtime response. The server's copy is the one cache; a client keeping
its own on top would add a second, invisible staleness (the Worker's reasoning)."""

_ADAPTERS: dict[str, TypeAdapter] = {
    "platform": TypeAdapter(StopId),
    "line": TypeAdapter(LineId),
    "station": TypeAdapter(StationId),
}


class BadRequest(Exception):
    def __init__(self, error: str, message: str):
        super().__init__(message)
        self.error = error
        self.message = message


def parse_ids(raw: str | None, *, param: str, kind: str, required: bool, max_count: int) -> list[str] | None:
    """A comma-separated id list, de-duplicated in order. None when absent (or
    blank) and not required."""
    if raw is None or raw.strip() == "":
        if required:
            raise BadRequest("bad-request", f"{param} is required: 1-{max_count} comma-separated {kind} ids.")
        return None
    ids = list(dict.fromkeys(raw.split(",")))
    if not 1 <= len(ids) <= max_count:
        raise BadRequest("bad-request", f"{param} takes 1-{max_count} comma-separated {kind} ids.")
    adapter = _ADAPTERS[kind]
    for value in ids:
        try:
            adapter.validate_python(value)
        except ValidationError:
            example = "SF:16992" if kind == "platform" else "SF:N" if kind == "line" else "embarcadero"
            raise BadRequest("bad-request", f"{value!r} is not a {kind} id (like {example}).") from None
    return ids


def parse_int(raw: str | None, *, param: str, default: int, maximum: int) -> int:
    """A positive integer, capped at ``maximum`` rather than refused above it:
    asking for more than we give is not an error worth failing a board over."""
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise BadRequest("bad-request", f"{param} must be a whole number from 1 to {maximum}.") from None
    if value < 1:
        raise BadRequest("bad-request", f"{param} must be a whole number from 1 to {maximum}.")
    return min(value, maximum)


def realtime_of(request: Request) -> Realtime:
    realtime = getattr(request.app.state, "realtime", None)
    if realtime is None:
        raise Unavailable()
    return realtime


class Unavailable(Exception):
    pass


def ok(model: BaseModel) -> JSONResponse:
    return JSONResponse(model.model_dump(mode="json", by_alias=True), headers=NO_STORE)


def problem(status: int, error: str, message: str) -> JSONResponse:
    body: dict[str, Any] = Problem(error=error, message=message).model_dump(mode="json", by_alias=True)
    return JSONResponse(body, status_code=status, headers=NO_STORE)


def answer(build: Callable[[], BaseModel]) -> JSONResponse:
    """Run ``build`` and turn the two expected failures into Problems. The
    endpoint parses before calling ``realtime_of``, so a bad request is a 400
    whether or not realtime is configured."""
    try:
        return ok(build())
    except BadRequest as bad:
        return problem(400, bad.error, bad.message)
    except Unavailable:
        return problem(503, "unavailable", "Realtime data is not configured on this server.")
