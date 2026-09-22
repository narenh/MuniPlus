"""``GET /api/lines`` and ``GET /api/lines/{id}``.

Hidden lines are included, with ``hidden`` set: whether to show one is the
client's call, and the editor needs them all.
"""

from fastapi import APIRouter, Request, Response

from ..models.api import LineDetailResponse, LinesResponse, Problem
from .stations import cached, network, not_found

router = APIRouter(prefix="/api", tags=["lines"])


@router.get("/lines", response_model=LinesResponse)
def list_lines(request: Request, response: Response):
    net = network(request)
    if (hit := cached(request, response, net.version)) is not None:
        return hit
    return net.lines()


@router.get("/lines/{line_id}", response_model=LineDetailResponse, responses={404: {"model": Problem}})
def get_line(line_id: str, request: Request, response: Response):
    net = network(request)
    line = net.line(line_id)
    if line is None:
        return not_found(f"No line {line_id!r}.")
    if (hit := cached(request, response, net.version)) is not None:
        return hit
    return LineDetailResponse(version=net.version, line=line)
