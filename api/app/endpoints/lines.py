"""``GET /api/lines``, ``GET /api/lines/{id}`` and ``GET /api/shapes``.

Hidden lines are included, with ``hidden`` set: whether to show one is the
client's call, and the editor needs them all.
"""

from fastapi import APIRouter, Request, Response

from ..models.api import LineDetailResponse, LinesResponse, Problem, ShapesResponse
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


@router.get("/shapes", response_model=ShapesResponse)
def list_shapes(request: Request, response: Response):
    # A few hundred KB that changes only when a snapshot refresh moves a point, so
    # its ETag is the shapes' own hash rather than the version: a client revalidates
    # with a 304 across every curation edit and fetches again only after 511 redraws.
    shapes, etag = network(request).shapes()
    if (hit := cached(request, response, etag)) is not None:
        return hit
    return shapes
