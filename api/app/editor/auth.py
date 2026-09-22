"""Login for ``/editor``: one password, a signed session cookie, and a throttle.

There is one editor and one password (``EDITOR_PASSWORD``), so a session is
nothing but proof that someone knew it: a cookie signed with ``SESSION_SECRET``
by ``itsdangerous``, checked by the routes themselves, so ``app.main`` needs no
session middleware. With no password, nobody can log in and ``/editor`` is
read-only. With no secret, sessions are refused outright: a default secret would
be one anybody can read in this public repo and sign their own cookie with.
"""

import hashlib
import hmac
import time
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer

from ..models.editor import LoginRequest
from ..settings import Settings
from .errors import ProblemError, ProblemRoute

COOKIE = "muniplus_editor"
COOKIE_PATH = "/editor"
SESSION_SECONDS = 14 * 24 * 3600
"""Two weeks: one owner on their own machines, so a login a week or so apart is
enough friction, and a leaked cookie still dies on its own."""
SALT = "muniplus-editor-session"

NO_PASSWORD = "no editor password is configured"
NO_SECRET = "no session secret is configured"


class Sessions:
    def __init__(self, settings: Settings):
        self._password = settings.editor_password
        self._secret = settings.session_secret
        self._signer = URLSafeTimedSerializer(self._secret, salt=SALT) if self._secret else None

    def unavailable(self) -> str | None:
        """Why nobody can log in right now, if they cannot."""
        if not self._password:
            return NO_PASSWORD
        if not self._secret:
            return NO_SECRET
        return None

    def password_ok(self, attempt: str) -> bool:
        # Comparing digests keeps the time independent of the password's length
        # too, which ``compare_digest`` on the raw strings would reveal.
        a = hashlib.sha256(attempt.encode()).digest()
        b = hashlib.sha256(self._password.encode()).digest()
        return bool(self._password) and hmac.compare_digest(a, b)

    def _fingerprint(self) -> str:
        # Ties a session to the password it was issued under, so changing the
        # password logs every session out. Keyed by the secret, so the cookie says
        # nothing about the password to whoever holds it.
        return hmac.new(self._secret.encode(), self._password.encode(), hashlib.sha256).hexdigest()[:32]

    def issue(self) -> str:
        if self._signer is None or not self._password:
            raise RuntimeError(self.unavailable())
        return self._signer.dumps({"pw": self._fingerprint()})

    def valid(self, token: str | None) -> bool:
        if not token or self._signer is None or not self._password:
            return False
        try:
            data = self._signer.loads(token, max_age=SESSION_SECONDS)
        except BadSignature:  # includes SignatureExpired
            return False
        return isinstance(data, dict) and hmac.compare_digest(str(data.get("pw", "")), self._fingerprint())

    def of(self, request: Request) -> bool:
        return self.valid(request.cookies.get(COOKIE))


# MARK: - Throttle

FREE_FAILURES = 5
"""Typos are free. After these, each further failure doubles the wait."""
BASE_DELAY = 2.0
MAX_DELAY = 600.0
"""The worst the owner can be locked out for, however many failures there were:
ten minutes. At the cap a guesser gets six tries an hour."""
FORGET_AFTER = 24 * 3600.0
MAX_CLIENTS = 10_000
"""Bounds the memory a flood of distinct addresses can take."""


class Throttle:
    """Failed logins per client, in memory. A restart forgets them, which is fine:
    a restart takes longer than most of the delays."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._failures: dict[str, tuple[int, float]] = {}

    def retry_after(self, client: str) -> float:
        """Seconds until ``client`` may try again; 0 when it may now."""
        entry = self._failures.get(client)
        if entry is None:
            return 0.0
        count, last = entry
        now = self._clock()
        if now - last > FORGET_AFTER:
            del self._failures[client]
            return 0.0
        if count < FREE_FAILURES:
            return 0.0
        wait = min(MAX_DELAY, BASE_DELAY * 2 ** (count - FREE_FAILURES))
        return max(0.0, last + wait - now)

    def failed(self, client: str) -> None:
        count, _ = self._failures.get(client, (0, 0.0))
        self._failures[client] = (count + 1, self._clock())
        if len(self._failures) > MAX_CLIENTS:
            oldest = min(self._failures, key=lambda c: self._failures[c][1])
            del self._failures[oldest]

    def succeeded(self, client: str) -> None:
        self._failures.pop(client, None)


def client_ip(request: Request) -> str:
    """The address to throttle: the last ``X-Forwarded-For`` entry, else the peer.

    The *last* entry, not the first. Every proxy appends the address it received
    the request from, so the last one was written by the proxy in front of us
    (Coolify's), while the first is whatever the client chose to send: a guesser
    putting a fresh made-up address there each time would never be throttled.
    Traefik and Caddy both drop an untrusted client's own ``X-Forwarded-For`` by
    default, in which case first and last are the same single entry anyway.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    entries = [e.strip() for e in forwarded.split(",") if e.strip()]
    if entries:
        return entries[-1]
    return request.client.host if request.client else "unknown"


# MARK: - Routes

router = APIRouter(prefix="/editor", route_class=ProblemRoute, include_in_schema=False)


def _editor(request: Request):
    return request.app.state.editor


def login_page(editor_dir: Path) -> Path:
    return editor_dir / "login.html"


@router.get("/login")
def login_form(request: Request):
    editor = _editor(request)
    if editor.sessions.of(request):
        return RedirectResponse("/editor/", status_code=303)
    return FileResponse(login_page(editor.editor_dir), headers={"Cache-Control": "no-cache"})


@router.post("/login", status_code=204)
async def login(body: LoginRequest, request: Request):
    # ``async`` on purpose: it runs on the event loop with no ``await`` between
    # checking the throttle and recording the result, so a burst of parallel
    # guesses from one address cannot all pass the check before any is counted,
    # as they could on the thread pool.
    editor = _editor(request)
    if reason := editor.sessions.unavailable():
        raise ProblemError(503, "unavailable", f"Login is off: {reason}.")
    client = client_ip(request)
    if (wait := editor.throttle.retry_after(client)) > 0:
        seconds = int(wait) + 1
        raise ProblemError(
            429,
            "unauthorized",
            f"Too many wrong passwords. Try again in {seconds} s.",
            headers={"Retry-After": str(seconds)},
        )
    if not editor.sessions.password_ok(body.password):
        editor.throttle.failed(client)
        raise ProblemError(401, "unauthorized", "Wrong password.")
    editor.throttle.succeeded(client)
    response = Response(status_code=204)
    set_session_cookie(response, editor.sessions.issue())
    return response


@router.post("/logout", status_code=204)
def logout():
    response = Response(status_code=204)
    set_session_cookie(response, "", max_age=0)
    return response


def set_session_cookie(response: Response, token: str, max_age: int = SESSION_SECONDS) -> None:
    response.set_cookie(
        COOKIE,
        token,
        max_age=max_age,
        path=COOKIE_PATH,
        # Secure: the site is only served over https (Coolify terminates TLS).
        # Browsers treat http://localhost as secure, so local runs still log in.
        secure=True,
        httponly=True,
        samesite="strict",
    )
