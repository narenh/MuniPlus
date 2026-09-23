"""``GET /api/v1/alerts?line=&station=&platforms=``

Each parameter is optional and takes a comma-separated list. With none, every
alert active now; otherwise those touching any line, station or platform named.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.models.api import AlertsResponse
from app.realtime.http import answer, parse_ids, realtime_of

router = APIRouter(prefix="/api/v1", tags=["realtime"])

MAX_IDS = 50


@router.get("/alerts", response_model=AlertsResponse)
def alerts(
    request: Request, line: str | None = None, station: str | None = None, platforms: str | None = None
) -> JSONResponse:
    def build():
        lines = parse_ids(line, param="line", kind="line", required=False, max_count=MAX_IDS)
        stations = parse_ids(station, param="station", kind="station", required=False, max_count=MAX_IDS)
        platform_ids = parse_ids(platforms, param="platforms", kind="platform", required=False, max_count=MAX_IDS)
        return realtime_of(request).alerts(lines=lines, stations=stations, platforms=platform_ids)

    return answer(build)
