"""The frontend's files, at ``/editor/`` (behind login) and ``/map/`` (public).

One frontend, ``api/editor/``, served twice. Its fetch paths are relative
(``api/state``), so the page at ``/editor/`` talks to ``/editor/api/`` and the one
at ``/map/`` to ``/map/api/``; each decides what to show from ``readOnly`` in the
state it loads.

Only ``index.html`` is gated. The scripts and styles are served to anyone: they
are the same files as in the public repo, and the login page must not need them.
"""

from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from .state import editor_of

# The page is small and changes with every frontend deploy; no-cache has the
# browser revalidate it (a 304 via its ETag) rather than run a stale one against a
# newer API.
NO_CACHE = {"Cache-Control": "no-cache"}

router = APIRouter(include_in_schema=False)


class _Revalidated(StaticFiles):
    """The scripts and styles, revalidated on every load like the page.

    Without a Cache-Control of our own, Cloudflare's default browser TTL gives them
    ``max-age=14400``, and a reload then runs the new page against four-hour-old
    modules: the shapes deploy was invisible on staging until a hard refresh. The
    files are small, and a revalidation is a 304 from their ETag."""

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers.update(NO_CACHE)
        return response


def _index(request: Request) -> FileResponse:
    return FileResponse(editor_of(request).editor_dir / "index.html", headers=NO_CACHE)


@router.get("/editor")
def editor_bare():
    # The trailing slash matters: without it, the page's relative ``api/state``
    # would resolve to ``/api/state``.
    return RedirectResponse("/editor/", status_code=308)


@router.get("/editor/")
def editor_page(request: Request):
    editor = editor_of(request)
    # No password: nobody can log in, so the page is served read-only to anyone
    # rather than hidden behind a login that cannot succeed.
    if editor.settings.editor_password and not editor.sessions.of(request):
        return RedirectResponse("/editor/login", status_code=303)
    return _index(request)


@router.get("/map")
def map_bare():
    return RedirectResponse("/map/", status_code=308)


@router.get("/map/")
def map_page(request: Request):
    return _index(request)


def mount_assets(app: FastAPI, editor_dir: Path) -> None:
    """Every folder of the frontend (``js/``, ``css/``, and any the frontend adds),
    under both prefixes. Folders are read once, at startup."""
    if not editor_dir.is_dir():
        return
    for folder in sorted(p for p in editor_dir.iterdir() if p.is_dir()):
        static = _Revalidated(directory=folder)
        for prefix in ("/editor", "/map"):
            app.mount(f"{prefix}/{folder.name}", static, name=f"{prefix.strip('/')}-{folder.name}")
