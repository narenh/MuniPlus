"""The Canopy Transit Editor: ``uvicorn transit_editor.main:app --workers 1``.

Startup: make sure the data checkout exists and is current, then build the
network from it. Each step degrades rather than stops the server. With no
checkout the editor and ``/map`` answer 503 and ``/health`` says why, and a pull
that fails keeps serving what is already on disk, because a server that will not
boot is harder to diagnose than one that says what is wrong.

Exactly one worker process: saves are serialised by a lock in this process and
the pending snapshot lives on its disk.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from .data.loader import load_checkout
from .data.repo import Repo, RepoError
from .editor.install import install_editor
from .endpoints import health, shapes
from .endpoints.http import DataUnavailable
from .models.api import Problem
from .settings import Settings

log = logging.getLogger("transit_editor")


def load_network(app: FastAPI) -> None:
    """Pull the data repo and (re)build ``app.state.network``. Safe to call again."""
    settings: Settings = app.state.settings
    repo = Repo(settings)
    try:
        repo.ensure_cloned()
        repo.pull()
    except RepoError as err:
        # Keep going: an existing checkout is still worth serving.
        log.warning("data sync failed, serving what is on disk: %s", err)
        app.state.data_error = f"sync failed: {err}"
    if not (repo.path / ".git").is_dir():
        # Nothing to load, and trying would only replace the sync error, which says
        # why (a rejected deploy key, say), with a less useful one about a missing
        # directory.
        app.state.network = None
        app.state.data_error = app.state.data_error or "no data checkout"
        return
    try:
        app.state.network = load_checkout(repo, settings.operators)
        app.state.data_error = None
        log.info("loaded data %s", app.state.network.version)
    except Exception as err:  # noqa: BLE001 - any failure here means "no data", reported by /health
        log.exception("could not load the data checkout")
        app.state.network = None
        app.state.data_error = f"load failed: {err}"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        load_network(app)
        yield

    app = FastAPI(
        title="Canopy Transit Editor",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.network = None
    app.state.data_error = None

    @app.exception_handler(DataUnavailable)
    async def _no_data(_request: Request, _exc: DataUnavailable):
        return JSONResponse(
            status_code=503,
            content=Problem(error="unavailable", message="The data is not loaded; see /health.").model_dump(),
            headers={"Cache-Control": "no-store"},
        )

    # The editor state carries every snapshot stop and pattern (hundreds of KB of
    # JSON) and compresses well.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    app.include_router(health.router)
    app.include_router(shapes.router)
    install_editor(app, settings)
    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()
