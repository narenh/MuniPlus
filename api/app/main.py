"""The Muni+ API server: ``uvicorn app.main:app --workers 1``.

Startup, in order: make sure the sf-transit checkout exists and is current, build
the network from it, then start the realtime pollers. Each step degrades rather
than stops the server. With no checkout the data endpoints answer 503 and
``/health`` says why, and a pull that fails keeps serving what is already on disk,
because a server that will not boot is harder to diagnose than one that says what
is wrong.

Exactly one worker process. The pollers take a lock in SQLite, so a second process
would serve but never poll, and more than one poller would spend the 511 budget
several times over. See PLAN.md.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from .data.loader import load_checkout
from .data.repo import Repo, RepoError
from .db import Database
from .endpoints import alerts, arrivals, health, lines, stations, vehicles
from .editor.install import install_editor
from .endpoints.stations import DataUnavailable
from .models.api import Problem
from .realtime.pollers import Pollers
from .realtime.state import Realtime
from .settings import Settings

log = logging.getLogger("muni")


def load_network(app: FastAPI) -> None:
    """Pull sf-transit and (re)build ``app.state.network``. Safe to call again."""
    settings: Settings = app.state.settings
    repo = Repo(settings)
    try:
        repo.ensure_cloned()
        repo.pull()
    except RepoError as err:
        # Keep going: an existing checkout is still worth serving.
        log.warning("sf-transit sync failed, serving what is on disk: %s", err)
        app.state.data_error = f"sync failed: {err}"
    try:
        app.state.network = load_checkout(repo, settings.operators)
        app.state.data_error = None
        log.info("loaded sf-transit %s", app.state.network.version)
    except Exception as err:  # noqa: BLE001 - any failure here means "no data", reported by /health
        log.exception("could not load sf-transit")
        app.state.network = None
        app.state.data_error = f"load failed: {err}"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        load_network(app)
        realtime = Realtime(lambda: app.state.network, settings, db=Database(settings.db_path))
        app.state.realtime = realtime
        pollers = Pollers(realtime) if settings.polling_enabled else None
        if pollers:
            await pollers.start()
        else:
            log.warning("realtime is off: set API_511_KEY, or FIXTURES=1 to replay recordings")
        try:
            yield
        finally:
            if pollers:
                await pollers.stop()

    app = FastAPI(title="Muni+", lifespan=lifespan, docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.state.settings = settings
    app.state.network = None
    app.state.data_error = None

    @app.exception_handler(DataUnavailable)
    async def _no_data(_request: Request, _exc: DataUnavailable):
        return JSONResponse(
            status_code=503,
            content=Problem(error="unavailable", message="Station data is not loaded; see /health.").model_dump(),
            headers={"Cache-Control": "no-store"},
        )

    # /api/stations is ~440 KB of JSON for the whole city and compresses about 8:1.
    # The app fetches it over cellular, so it is compressed here, not left to
    # whatever proxy happens to be in front.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    for module in (stations, lines, arrivals, vehicles, alerts, health):
        app.include_router(module.router)
    install_editor(app, settings)
    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()
