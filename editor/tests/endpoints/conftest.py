from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from transit_editor.data import loader
from transit_editor.endpoints import shapes

FIXTURE = Path(__file__).parent.parent / "fixtures" / "transit"
VERSION = "0123456789abcdef0123456789abcdef01234567"


def build_app(network=None) -> FastAPI:
    """A bare app with only the shapes router."""
    app = FastAPI()
    app.include_router(shapes.router)
    app.state.network = network or loader.load(FIXTURE, VERSION)
    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_app())
