"""The frontend's files, at ``/editor/`` (behind login) and ``/map/`` (public).

One frontend, ``api/editor/``, served twice. Its fetch paths are relative
(``api/state``), so the page at ``/editor/`` talks to ``/editor/api/`` and the one
at ``/map/`` to ``/map/api/``; each decides what to show from ``readOnly`` in the
state it loads.

Only ``index.html`` is gated. The scripts and styles are served to anyone: they
are the same files as in the public repo, and the login page must not need them.

Cache busting: the page is served with its script and stylesheet under
``v/<version>/``, a hash of every frontend file. The modules import each other
by relative path, so the whole graph loads from that one versioned folder, and a
deploy that changes any file changes every URL. Those URLs never change content,
so they are cached for good. That is also what makes a deploy show up at once:
on staging, Cloudflare replaced the ``no-cache`` these files used to carry with
its own ``max-age=14400``, so a reload ran four-hour-old modules. A browser can
hold an old file for as long as it likes under an old version's URL, and never
asks for that URL again. The page itself stays ``no-cache``, and Cloudflare
leaves that alone. ``fetch("api/state")`` resolves against the page's URL, not
the module's, so the API paths are unaffected.
"""

import functools
import hashlib
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from ..endpoints.stations import etag_of, not_modified
from .state import editor_of

# The page is small and changes with every frontend deploy; no-cache has the
# browser revalidate it (a 304 via its ETag) rather than run a stale one against a
# newer API.
NO_CACHE = {"Cache-Control": "no-cache"}
FOREVER = {"Cache-Control": "public, max-age=31536000, immutable"}
VERSIONED = "v"

# The references index.html makes to its own assets, rewritten into the
# versioned folder. Each must be found: a page edited so one no longer matches
# would silently stop busting that asset.
ASSET_REFS = ('href="css/', 'src="js/', 'src="img/')

router = APIRouter(include_in_schema=False)


class _Assets(StaticFiles):
    """Static files with a Cache-Control of our own."""

    def __init__(self, *, headers: dict[str, str], **kwargs):
        super().__init__(**kwargs)
        self.headers = headers

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers.update(self.headers)
        return response


@functools.cache
def asset_version(editor_dir: Path) -> str:
    """A hash of every file the page loads. The frontend ships in the image and
    never changes while the server runs, so it is computed once."""
    digest = hashlib.sha256()
    for path in sorted(p for p in editor_dir.rglob("*") if p.is_file()):
        digest.update(path.relative_to(editor_dir).as_posix().encode() + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


@functools.cache
def _page(editor_dir: Path) -> tuple[str, str]:
    """``index.html`` pointing into the versioned folder, and its ETag."""
    version = asset_version(editor_dir)
    html = (editor_dir / "index.html").read_text(encoding="utf-8")
    for ref in ASSET_REFS:
        if ref not in html:
            raise RuntimeError(f"index.html no longer contains {ref!r}; update pages.ASSET_REFS")
        attr, path = ref.split('"')
        html = html.replace(ref, f'{attr}"{VERSIONED}/{version}/{path}')
    return html, hashlib.sha256(html.encode()).hexdigest()[:16]


def _index(request: Request) -> Response:
    html, etag = _page(editor_of(request).editor_dir)
    headers = {**NO_CACHE, "ETag": etag_of(etag)}
    if not_modified(request, etag):
        return Response(status_code=304, headers=headers)
    return HTMLResponse(html, headers=headers)


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
    version = asset_version(editor_dir)
    for folder in sorted(p for p in editor_dir.iterdir() if p.is_dir()):
        # Unversioned too, revalidated on every load, for anything that still asks
        # for a file by its plain path.
        current = _Assets(directory=folder, headers=FOREVER)
        plain = _Assets(directory=folder, headers=NO_CACHE)
        for prefix in ("/editor", "/map"):
            name = f"{prefix.strip('/')}-{folder.name}"
            app.mount(f"{prefix}/{VERSIONED}/{version}/{folder.name}", current, name=f"{name}-versioned")
            app.mount(f"{prefix}/{folder.name}", plain, name=name)
    for prefix in ("/editor", "/map"):
        # After the mounts above, so only a version this server does not have
        # gets here.
        app.add_api_route(f"{prefix}/{VERSIONED}/{{version}}/{{path:path}}", _other_version, include_in_schema=False)


def _other_version(version: str, path: str) -> Response:
    """Another deploy's asset. Coolify runs the old container alongside the new
    one until the new one is healthy, and requests alternate between them, so the
    new page's assets reach the old server for a minute or two. A 404 there was
    cached: by Cloudflare's edge for minutes, and by the browser for Cloudflare's
    four hours. A 503 with no-store is cached by neither, and the next reload
    lands on the server that has it."""
    return Response(
        f"Asset version {version} is not on this server; reload.",
        status_code=503,
        media_type="text/plain",
        headers={"Cache-Control": "no-store", "Retry-After": "5"},
    )
