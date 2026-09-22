from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.data import loader
from app.endpoints import lines, stations

FIXTURE = Path(__file__).parent.parent / "fixtures" / "transit"
VERSION = "0123456789abcdef0123456789abcdef01234567"


def build_app(network=None, realtime=None) -> FastAPI:
    """A bare app with only this track's routers: ``app.main`` is the integrator's."""
    app = FastAPI()
    app.include_router(stations.router)
    app.include_router(lines.router)
    app.state.network = network or loader.load(FIXTURE, VERSION)
    if realtime is not None:
        app.state.realtime = realtime
    return app


@pytest.fixture
def make_app():
    return build_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_app())
