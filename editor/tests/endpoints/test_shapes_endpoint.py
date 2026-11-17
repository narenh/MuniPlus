"""``/api/shapes`` and ``/map/api/shapes`` against tests/fixtures/transit."""

import pytest

from transit_editor.endpoints.http import etag_of
from transit_editor.models.api import ShapesResponse

VERSION = "0123456789abcdef0123456789abcdef01234567"
PATHS = ("/api/shapes", "/map/api/shapes")


@pytest.mark.parametrize("path", PATHS)
def test_shapes(client, path):
    r = client.get(path)
    assert r.status_code == 200
    body = r.json()
    ShapesResponse.model_validate(body)
    # The fixture's point halfway along Market is on the line and is dropped; the
    # corner at Castro is not.
    assert body == {
        "shapes": {
            "SF:F1": [[-122.434979, 37.762576], [-122.435139, 37.76262], [-122.429214, 37.76725]],
        }
    }


@pytest.mark.parametrize("path", PATHS)
def test_shapes_etag_is_the_shapes_not_the_version(client, path):
    r = client.get(path)
    etag = r.headers["etag"]
    assert etag != etag_of(VERSION)
    assert r.headers["cache-control"] == "no-cache"
    assert client.get(path, headers={"If-None-Match": etag}).status_code == 304
    assert client.get(path, headers={"If-None-Match": etag_of(VERSION)}).status_code == 200


def test_the_app_api_is_gone(client):
    for path in ("/api/v1/shapes", "/api/v1/stations", "/api/v1/lines", "/api/v1/arrivals", "/api/v1/vehicles"):
        assert client.get(path).status_code == 404
