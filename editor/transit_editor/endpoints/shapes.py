"""``GET /api/shapes`` and ``GET /map/api/shapes``: every line direction's
path, for the map. Public under both prefixes, like ``/map/api/state``: the
frontend fetches the relative ``api/shapes``, and the data is public anyway.
"""

from fastapi import APIRouter, Request, Response

from ..models.api import ShapesResponse
from .http import cached, network

router = APIRouter(tags=["shapes"])


def list_shapes(request: Request, response: Response):
    # A few hundred KB that changes only when a snapshot refresh moves a point, so
    # its ETag is the shapes' own hash rather than the version: a client revalidates
    # with a 304 across every curation edit and fetches again only after 511 redraws.
    shapes, etag = network(request).shapes()
    if (hit := cached(request, response, etag)) is not None:
        return hit
    return shapes


for prefix in ("/api", "/map/api"):
    router.add_api_route(f"{prefix}/shapes", list_shapes, methods=["GET"], response_model=ShapesResponse)
