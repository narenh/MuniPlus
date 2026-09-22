"""``GET /api/arrivals?platforms=a,b&limit=6``"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.models.api import ArrivalsResponse
from app.realtime.http import answer, parse_ids, parse_int, realtime_of

router = APIRouter(prefix="/api", tags=["realtime"])

MAX_PLATFORMS = 50
"""A station board asks for all its platforms at once. The cap stops one request
walking the whole city out of us (the Worker's reasoning for its own cap of 40)."""
DEFAULT_LIMIT = 6
MAX_LIMIT = 30
"""Per platform, and a cap rather than a refusal. The busiest platform in the
fixture (SF:15621) has 18 arrivals in the next 30 minutes and 33 in the hour,
so 30 is about an hour at the busiest stop: further out than a board shows."""


@router.get("/arrivals", response_model=ArrivalsResponse)
def arrivals(request: Request, platforms: str | None = None, limit: str | None = None) -> JSONResponse:
    def build():
        ids = parse_ids(platforms, param="platforms", kind="platform", required=True, max_count=MAX_PLATFORMS)
        count = parse_int(limit, param="limit", default=DEFAULT_LIMIT, maximum=MAX_LIMIT)
        return realtime_of(request).arrivals(ids, count)

    return answer(build)
