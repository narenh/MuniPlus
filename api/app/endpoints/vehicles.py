"""``GET /api/v1/v1/vehicles?line=a,b``"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.models.api import VehiclesResponse
from app.realtime.http import answer, parse_ids, realtime_of

router = APIRouter(prefix="/api/v1", tags=["realtime"])

MAX_LINES = 100
"""SF has 68 lines, so every line fits, with room for a second operator."""


@router.get("/vehicles", response_model=VehiclesResponse)
def vehicles(request: Request, line: str | None = None) -> JSONResponse:
    def build():
        lines = parse_ids(line, param="line", kind="line", required=False, max_count=MAX_LINES)
        return realtime_of(request).vehicles(set(lines) if lines is not None else None)

    return answer(build)
