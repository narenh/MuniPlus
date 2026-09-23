"""Login, sessions, the throttle, and what needs a session."""

import json

import pytest
from itsdangerous import URLSafeTimedSerializer

from app.editor import auth
from app.editor.auth import COOKIE, MAX_DELAY, SALT, Throttle
from app.editor.pages import asset_version


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def set_cookie(res) -> str:
    return res.headers.get("set-cookie", "")


# MARK: - Logging in


def test_login_sets_a_locked_down_session_cookie(world):
    res = world.client.post("/editor/login", json={"password": world.password})
    assert res.status_code == 204
    cookie = set_cookie(res)
    assert cookie.startswith(f"{COOKIE}=")
    lowered = cookie.lower()
    for attribute in ("httponly", "secure", "samesite=strict", "path=/editor", f"max-age={auth.SESSION_SECONDS}"):
        assert attribute in lowered, cookie
    assert world.client.get("/editor/api/state").status_code == 200


def test_wrong_password(world):
    res = world.client.post("/editor/login", json={"password": "hunter2"})
    assert res.status_code == 401
    assert res.json() == {"error": "unauthorized", "message": "Wrong password."}
    assert COOKIE not in set_cookie(res)
    assert world.client.get("/editor/api/state").status_code == 401


def test_logout_clears_the_cookie(world):
    world.login()
    res = world.client.post("/editor/logout", json={})
    assert res.status_code == 204
    assert "max-age=0" in set_cookie(res).lower()
    assert world.client.get("/editor/api/state").status_code == 401


def test_login_page_and_redirects(world):
    page = world.client.get("/editor/login")
    assert page.status_code == 200
    assert 'id="login"' in page.text
    res = world.client.get("/editor/", follow_redirects=False)
    assert (res.status_code, res.headers["location"]) == (303, "/editor/login")
    assert world.client.get("/editor", follow_redirects=False).headers["location"] == "/editor/"

    world.login()
    assert world.client.get("/editor/").status_code == 200
    assert world.client.get("/editor/login", follow_redirects=False).headers["location"] == "/editor/"


def test_assets_need_no_session(world):
    for path in ("/editor/js/main.js", "/editor/css/app.css", "/map/js/main.js"):
        assert world.client.get(path).status_code == 200, path


def test_the_page_loads_its_assets_from_a_versioned_folder(world):
    version = asset_version(world.app.state.editor.editor_dir)
    page = world.client.get("/map/")
    assert page.headers["cache-control"] == "no-cache"
    assert f'src="v/{version}/js/main.js"' in page.text
    assert f'href="v/{version}/css/app.css"' in page.text
    assert 'src="js/' not in page.text and 'href="css/' not in page.text
    assert world.client.get("/map/", headers={"If-None-Match": page.headers["etag"]}).status_code == 304

    # Relative imports inside main.js resolve into the same folder.
    for path in (f"/map/v/{version}/js/main.js", f"/map/v/{version}/js/map.js", f"/editor/v/{version}/css/app.css"):
        res = world.client.get(path)
        assert res.status_code == 200, path
        assert res.headers["cache-control"] == "public, max-age=31536000, immutable", path

    # Another deploy's version, as the old container sees the new page's assets
    # mid-deploy: refused in a way nothing caches, rather than a cacheable 404.
    other = world.client.get("/map/v/000000000000/js/main.js")
    assert other.status_code == 503
    assert other.headers["cache-control"] == "no-store"


def test_plain_asset_paths_are_revalidated(world):
    for path in ("/editor/js/main.js", "/map/css/app.css"):
        res = world.client.get(path)
        assert res.headers["cache-control"] == "no-cache", path
        again = world.client.get(path, headers={"If-None-Match": res.headers["etag"]})
        assert again.status_code == 304, path


def test_the_version_follows_the_files(tmp_path):
    for name, text in (("index.html", '<link href="css/a.css"><script src="js/m.js"></script>'), ("js/m.js", "1"), ("css/a.css", "")):
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_text(text)
    before = asset_version.__wrapped__(tmp_path)
    assert asset_version.__wrapped__(tmp_path) == before
    (tmp_path / "js/m.js").write_text("2")
    assert asset_version.__wrapped__(tmp_path) != before


def test_forged_and_foreign_cookies_are_refused(world):
    forged = URLSafeTimedSerializer("not-the-secret", salt=SALT).dumps({"pw": "whatever"})
    world.client.cookies.set(COOKIE, forged, domain="testserver", path="/editor")
    assert world.client.get("/editor/api/state").status_code == 401

    world.client.cookies.clear()
    world.login()
    token = world.client.cookies.get(COOKIE)
    world.client.cookies.clear()
    world.client.cookies.set(COOKIE, token[:-2] + ("AA" if token[-2:] != "AA" else "BB"), domain="testserver", path="/editor")
    assert world.client.get("/editor/api/state").status_code == 401


def test_changing_the_password_ends_sessions(world):
    world.login()
    world.app.state.editor.sessions = auth.Sessions(world.settings.model_copy(update={"editor_password": "new one"}))
    assert world.client.get("/editor/api/state").status_code == 401


def test_sessions_expire(world, monkeypatch):
    world.login()
    monkeypatch.setattr(auth, "SESSION_SECONDS", -1)
    assert world.client.get("/editor/api/state").status_code == 401


# MARK: - Throttle


def test_throttle_backs_off_and_recovers(world):
    clock = Clock()
    world.app.state.editor.throttle = Throttle(clock)

    def attempt(password, ip="203.0.113.7"):
        return world.client.post("/editor/login", json={"password": password}, headers={"X-Forwarded-For": ip})

    for _ in range(auth.FREE_FAILURES):
        assert attempt("wrong").status_code == 401
    # The next one waits, even with the right password: a throttled attempt is
    # never checked, so it tells a guesser nothing.
    blocked = attempt(world.password)
    assert blocked.status_code == 429
    assert blocked.json()["error"] == "unauthorized"
    assert int(blocked.headers["retry-after"]) >= 1
    # Another address is not held up by this one.
    assert attempt("wrong", ip="198.51.100.1").status_code == 401

    clock.now += auth.BASE_DELAY
    assert attempt("wrong").status_code == 401
    clock.now += auth.BASE_DELAY  # the wait doubled, so this is not enough
    assert attempt(world.password).status_code == 429
    clock.now += auth.BASE_DELAY
    assert attempt(world.password).status_code == 204

    # Success forgets the failures.
    assert attempt("wrong").status_code == 401


def test_throttle_never_locks_out_for_more_than_the_cap():
    clock = Clock()
    throttle = Throttle(clock)
    for _ in range(200):
        throttle.failed("a")
    assert throttle.retry_after("a") == pytest.approx(MAX_DELAY)
    clock.now += MAX_DELAY
    assert throttle.retry_after("a") == 0
    clock.now += auth.FORGET_AFTER + 1
    assert throttle.retry_after("a") == 0
    assert "a" not in throttle._failures


def test_throttle_is_keyed_by_the_address_our_proxy_saw(world):
    # A guesser choosing its own X-Forwarded-For only controls the first entry;
    # the proxy appends the real one last.
    world.app.state.editor.throttle = Throttle(Clock())
    for i in range(auth.FREE_FAILURES):
        res = world.client.post(
            "/editor/login", json={"password": "wrong"}, headers={"X-Forwarded-For": f"10.0.0.{i}, 203.0.113.7"}
        )
        assert res.status_code == 401
    res = world.client.post(
        "/editor/login", json={"password": "wrong"}, headers={"X-Forwarded-For": "10.9.9.9, 203.0.113.7"}
    )
    assert res.status_code == 429


# MARK: - Misconfiguration


def test_no_password_serves_the_editor_read_only(make):
    world = make(editor_password="")
    res = world.client.post("/editor/login", json={"password": ""})
    assert res.status_code == 503
    assert res.json()["error"] == "unavailable"
    assert "no editor password is configured" in res.json()["message"]
    assert COOKIE not in set_cookie(res)

    assert world.client.get("/editor/", follow_redirects=False).status_code == 200
    state = world.state()
    assert (state["readOnly"], state["readOnlyReason"]) == (True, "no editor password is configured")
    assert world.client.get("/editor/api/history").status_code == 200
    # Reading is open; nothing that writes or computes on a posted body is.
    curation = state["curation"]
    assert world.client.post("/editor/api/validate", json={"curation": curation}).status_code == 401
    assert world.save(curation).status_code == 401


def test_no_secret_issues_no_sessions(make):
    world = make(session_secret="")
    res = world.client.post("/editor/login", json={"password": world.password})
    assert res.status_code == 503
    assert "no session secret is configured" in res.json()["message"]
    assert COOKIE not in set_cookie(res)
    assert world.client.get("/editor/", follow_redirects=False).status_code == 303
    assert world.client.get("/editor/api/state").status_code == 401


def test_sessions_refuse_to_sign_without_a_secret(world):
    sessions = auth.Sessions(world.settings.model_copy(update={"session_secret": ""}))
    with pytest.raises(RuntimeError):
        sessions.issue()
    assert not sessions.valid("anything")


# MARK: - What needs a session


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/editor/api/state"), ("POST", "/editor/api/validate"), ("POST", "/editor/api/save"), ("GET", "/editor/api/history")],
)
def test_api_needs_a_session(world, method, path):
    res = world.client.request(method, path, json={})
    assert res.status_code == 401
    assert res.json()["error"] == "unauthorized"


def test_posts_must_be_json(world):
    # The CSRF defence: a cross-site form can post these content types without a
    # preflight; application/json it cannot.
    for content_type in ("application/x-www-form-urlencoded", "text/plain", "multipart/form-data; boundary=x"):
        res = world.client.post("/editor/login", content=b"password=x", headers={"Content-Type": content_type})
        assert res.status_code == 415, content_type
    world.login()
    state = world.state()
    body = json.dumps({"curation": state["curation"]})
    res = world.client.post("/editor/api/validate", content=body, headers={"Content-Type": "text/plain"})
    assert res.status_code == 415
    assert res.json()["error"] == "bad-request"
    res = world.client.post(
        "/editor/api/validate", content=body, headers={"Content-Type": "application/json; charset=utf-8"}
    )
    assert res.status_code == 200
