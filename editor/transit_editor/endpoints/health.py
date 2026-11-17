"""``GET /health``: whether the data checkout is loaded.

Reads only what is already in memory, never git and never the relay, so checking
it costs nothing and cannot itself fail the thing it reports on. Always 200: the
container's healthcheck is liveness only.
"""

from fastapi import APIRouter, Request, Response

from ..models.api import Health

router = APIRouter(tags=["health"])


@router.get("/health", response_model=Health)
def health(request: Request, response: Response) -> Health:
    state = request.app.state
    network = getattr(state, "network", None)
    problems: list[str] = []
    if network is None:
        problems.append(f"no data: {getattr(state, 'data_error', None) or 'not loaded'}")
    response.headers["Cache-Control"] = "no-store"
    return Health(ok=not problems, version=network.version if network else None, problems=problems)
