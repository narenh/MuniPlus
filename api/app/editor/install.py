"""The one call ``app.main`` makes to put the editor and ``/map`` on the app."""

from pathlib import Path

from fastapi import FastAPI

from ..endpoints import map as map_endpoint
from ..settings import Settings
from . import api, auth, pages
from .state import EDITOR_DIR, Editor


def install_editor(app: FastAPI, settings: Settings, *, editor_dir: Path = EDITOR_DIR) -> Editor:
    """Add ``/editor`` (login, pages, ``/editor/api/*``) and ``/map`` (page and
    ``/map/api/state``) to ``app``, and keep their state on ``app.state.editor``.

    Expects ``app.state.network`` to hold the network loaded from the checkout at
    ``settings.transit_dir`` by the time requests arrive; a save replaces it.
    Cloning the checkout stays with the caller, which already does it to load
    that network.
    """
    editor = Editor(settings, editor_dir=editor_dir)
    app.state.editor = editor
    app.include_router(auth.router)
    app.include_router(api.router)
    app.include_router(map_endpoint.router)
    app.include_router(pages.router)
    pages.mount_assets(app, editor_dir)
    return editor
