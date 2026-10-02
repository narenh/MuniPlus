"""``/api/v1/speech``: arrivals as a sentence to read aloud."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rt_support import FakeNetwork, fixture_bytes

from app.endpoints import speech
from app.models.api import Arrival
from app.realtime.state import Realtime

NOW = 1790113897

NAMES = {
    "SF:J": ("J", "J Church"),
    "SF:K": ("K", "K Ingleside"),
    "SF:L": ("L", "L Taraval"),
    "SF:M": ("M", "M Ocean View"),
    "SF:N": ("N", "N Judah"),
    "SF:9": ("9", "9 San Bruno"),
    "SF:14R": ("14R", "14R Mission Rapid"),
    "SF:52": ("52", "52 Excelsior"),
}


class NamedNetwork(FakeNetwork):
    def lines(self):
        return SimpleNamespace(lines=[SimpleNamespace(id=i, short_name=s, name=n) for i, (s, n) in NAMES.items()])


def make_app(realtime: Realtime | None, network=None) -> FastAPI:
    app = FastAPI()
    app.include_router(speech.router)
    app.state.network = network
    if realtime is not None:
        app.state.realtime = realtime
    return app


@pytest.fixture
def client(settings, db):
    network = NamedNetwork()
    realtime = Realtime(lambda: network, settings, db=db, clock=lambda: NOW)
    realtime.ingest("SF", "tripupdates", fixture_bytes("tripupdates-1.pb.gz"), fetched_at=NOW)
    return TestClient(make_app(realtime, network))


def say(client, **params) -> str:
    response = client.get("/api/v1/speech", params=params)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["cache-control"] == "no-store"
    return response.text


def test_one_clause_per_line(client):
    # Church outbound: K at 2 min, J and N at 4.
    assert say(client, stop="SF:17217") == (
        "There's a K Ingleside in 2 minutes, a J Church in 4 minutes, and an N Judah in 4 minutes."
    )


def test_a_line_coming_again_is_one_clause(client):
    assert say(client, stop="SF:13243") == "There's a 9 San Bruno in 2, 12 and 24 minutes."


def test_due_now(client):
    assert say(client, stop="SF:15621", limit="2") == "There's a 52 Excelsior now and a 14R Mission Rapid in 2 minutes."


def test_trips_ending_here_are_not_spoken(client):
    # Embarcadero inbound: all but the N end here, and nobody boards those.
    assert say(client, stop="SF:16992", limit="1") == "There's an N Judah in 5 minutes."


def test_nothing_due(client):
    assert say(client, stop="SF:99999") == "Nothing's due here right now."


def test_names_fall_back_to_the_line_id(settings, db):
    realtime = Realtime(lambda: None, settings, db=db, clock=lambda: NOW)
    realtime.ingest("SF", "tripupdates", fixture_bytes("tripupdates-1.pb.gz"), fetched_at=NOW)
    client = TestClient(make_app(realtime))
    assert say(client, stop="SF:17217", limit="1") == "There's a K in 2 minutes."


@pytest.mark.parametrize("params", [{}, {"stop": "15621"}, {"stop": "SF:15621,SF:13243"}, {"stop": "SF:15621", "limit": "0"}])
def test_bad_requests_are_sentences(client, params):
    response = client.get("/api/v1/speech", params=params)
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/plain")
    assert not response.text.startswith("{")


def test_no_realtime(settings, db):
    client = TestClient(make_app(None))
    response = client.get("/api/v1/speech", params={"stop": "SF:15621"})
    assert response.status_code == 503
    assert response.text == "Predictions aren't available right now."


def test_before_the_first_fetch(settings, db):
    client = TestClient(make_app(Realtime(lambda: None, settings, db=db, clock=lambda: NOW)))
    assert client.get("/api/v1/speech", params={"stop": "SF:15621"}).status_code == 503


@pytest.mark.parametrize("short, expected", [
    ("1", "a"), ("1X", "a"), ("8", "an"), ("8AX", "an"), ("18", "an"), ("14R", "a"), ("714", "a"),
    ("K", "a"), ("J", "a"), ("M", "an"), ("N", "an"), ("L", "an"), ("F", "an"), ("FBUS", "an"), ("T", "a"),
])  # fmt: skip
def test_article(short, expected):
    assert speech.article(short) == expected


def arrival(line: str, minutes: float) -> Arrival:
    return Arrival(line=line, direction=1, headsign="x", time=int(NOW + minutes * 60), kind="arrival",
                   terminates=False, trip=f"SF:{line[3:]}_{minutes}", vehicle=None)  # fmt: skip


@pytest.mark.parametrize("minutes, expected", [
    ([1], "There's a K in 1 minute."),
    ([1.9], "There's a K in 1 minute."),
    ([0.5], "There's a K now."),
    ([0, 6], "There's a K now and in 6 minutes."),
    ([0, 6, 12], "There's a K now and in 6 and 12 minutes."),
    ([3, 6, 12, 20], "There's a K in 3, 6, 12 and 20 minutes."),
])  # fmt: skip
def test_when(minutes, expected):
    assert speech.sentence([arrival("SF:K", m) for m in minutes], NOW, {}) == expected
