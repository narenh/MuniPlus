"""``GET /api/stations`` and ``GET /api/stations/{id}``.

Both read ``request.app.state.network``, which is replaced whole on a reload, so a
handler takes it once and uses that one value throughout.
"""

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from ..data.network import Network
from ..models.api import Problem, StationDetailResponse, StationsResponse

router = APIRouter(prefix="/api", tags=["stations"])

# MARK: - Shared with lines.py

# The ETag is the sf-transit commit: every byte of these responses is a function of
# it, so a client holding the current version can skip the body. It is quoted, as
# RFC 9110 requires; ``no-cache`` makes clients revalidate every time instead of
# guessing a freshness lifetime, since a data edit can land at any moment.
CACHE_CONTROL = "no-cache"


def etag_of(version: str) -> str:
    return f'"{version}"'


def not_modified(request: Request, version: str) -> bool:
    """Whether ``If-None-Match`` already names this version. Lenient about ``W/`` and
    missing quotes, since for a 304 a false negative only costs a body."""
    header = request.headers.get("if-none-match")
    if not header:
        return False
    for tag in header.split(","):
        tag = tag.strip().removeprefix("W/").strip('"')
        if tag == "*" or tag == version:
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


def network(request: Request) -> Network:
    return request.app.state.network


# MARK: - Stations


@router.get("/stations", response_model=StationsResponse)
def list_stations(request: Request, response: Response):
    net = network(request)
    if (hit := cached(request, response, net.version)) is not None:
        return hit
    return net.stations()


@router.get(
    "/stations/{station_id}",
    response_model=StationDetailResponse,
    responses={308: {"description": "A former id: redirects to the current one."}, 404: {"model": Problem}},
)
def get_station(station_id: str, request: Request):
    # No ETag here: ``alerts`` change with the realtime feed, not with the version.
    net = network(request)
    station = net.station(station_id)
    if station is None:
        if current := net.current_id(station_id):
            # Relative, so it stays on whatever scheme and host the proxy in front
            # was reached by. 308 rather than 301 so a client never rewrites the method.
            path = request.url.path.removesuffix(station_id) + current
            if request.url.query:
                path += "?" + request.url.query
            return Response(status_code=308, headers={"Location": path})
        return not_found(f"No station {station_id!r}.")
    realtime = getattr(request.app.state, "realtime", None)
    if realtime is not None:
        station = station.model_copy(update={"alerts": realtime.alerts(stations={station_id}).alerts})
    return StationDetailResponse(version=net.version, station=station)
