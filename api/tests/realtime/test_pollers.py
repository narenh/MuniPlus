"""The pollers: restart without re-fetching, fixtures mode, and one poller only."""

import time

import httpx
import pytest
from rt_support import FIXTURES, fixture_bytes, make_settings, until

from app.db import Database
from app.realtime.pollers import FixtureReplay, Pollers
from app.realtime.state import Realtime
from app.upstream.client511 import Client511
from app.upstream.gtfsrt import FEEDS

PAYLOADS = {
    "tripupdates": fixture_bytes("tripupdates-1.pb.gz"),
    "vehiclepositions": fixture_bytes("vehiclepositions-1.pb.gz"),
    "servicealerts": fixture_bytes("servicealerts.pb.gz"),
}


class Upstream:
    """A fake 511 serving the recorded feeds and counting every request."""

    def __init__(self, status: int = 200):
        self.requests: list[str] = []
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        feed = request.url.path.rsplit("/", 1)[-1]
        self.requests.append(feed)
        if self.status != 200:
            return httpx.Response(self.status)
        return httpx.Response(200, content=PAYLOADS[feed], headers={"RateLimit-Remaining": "50"})


def build(tmp_path, upstream: Upstream | None = None, *, db: Database | None = None, **settings_overrides):
    settings = make_settings(tmp_path, **({"api_511_key": "test-key-not-real"} | settings_overrides))
    db = db or Database(settings.db_path)
    realtime = Realtime(lambda: None, settings, db=db)
    built: list[Client511] = []

    def factory():
        if upstream is None:
            raise AssertionError("the 511 client must not be built here")
        client = Client511(settings, db, transport=httpx.MockTransport(upstream))
        built.append(client)
        return client

    return realtime, factory, built


def persist(db: Database, age: dict[str, int]):
    now = int(time.time())
    for feed, seconds in age.items():
        db.save_payload("SF", feed, PAYLOADS[feed], now - seconds)


async def test_restart_with_fresh_payloads_makes_no_calls(tmp_path):
    upstream = Upstream()
    realtime, factory, _ = build(tmp_path, upstream)
    persist(realtime.db, {"tripupdates": 30, "vehiclepositions": 170, "servicealerts": 1100})
    saved = {feed: realtime.db.payload_fetched_at("SF", feed) for feed in FEEDS}

    pollers = Pollers(realtime, client_factory=factory)
    await pollers.start()
    assert pollers.polling
    await until(lambda: len(pollers.next_poll) == 3)
    # Each feed is next due one interval after its saved copy, not now.
    assert pollers.next_poll == {("SF", f): saved[f] + realtime.interval(f) for f in FEEDS}
    await pollers.stop()

    assert upstream.requests == []
    assert realtime.db.calls() == []
    assert all(realtime.fetched_at("SF", feed) is not None for feed in FEEDS)
    assert realtime.arrivals(["SF:15621"], limit=1, now=1790113897).platforms["SF:15621"]


async def test_only_stale_feeds_are_fetched_on_startup(tmp_path):
    upstream = Upstream()
    realtime, factory, _ = build(tmp_path, upstream)
    persist(realtime.db, {"tripupdates": 181, "vehiclepositions": 60, "servicealerts": 60})
    before = realtime.db.payload_fetched_at("SF", "tripupdates")

    pollers = Pollers(realtime, client_factory=factory)
    await pollers.start()
    await until(lambda: len(upstream.requests) == 1 and realtime.db.payload_fetched_at("SF", "tripupdates") > before)
    await pollers.stop()

    assert upstream.requests == ["tripupdates"]
    assert [(c.feed, c.status, c.rate_limit_remaining) for c in realtime.db.calls()] == [("tripupdates", 200, 50)]


async def test_nothing_persisted_fetches_everything_once(tmp_path):
    upstream = Upstream()
    realtime, factory, _ = build(tmp_path, upstream)
    pollers = Pollers(realtime, client_factory=factory)
    await pollers.start()
    await until(lambda: all(realtime.db.payload_fetched_at("SF", f) for f in FEEDS))
    await pollers.stop()
    assert sorted(upstream.requests) == sorted(FEEDS)


async def test_a_failing_feed_keeps_its_data_and_waits_its_interval(tmp_path):
    upstream = Upstream(status=500)
    realtime, factory, _ = build(tmp_path, upstream)
    persist(realtime.db, {"tripupdates": 500, "vehiclepositions": 60, "servicealerts": 60})
    held = realtime.db.payload_fetched_at("SF", "tripupdates")

    pollers = Pollers(realtime, client_factory=factory)
    await pollers.start()
    await until(lambda: realtime.health()[0]["SF:tripupdates"].last_error is not None)
    await until(lambda: pollers.next_poll.get(("SF", "tripupdates"), 0) > time.time())
    next_try = pollers.next_poll[("SF", "tripupdates")] - time.time()
    await pollers.stop()
    assert 170 < next_try <= 180

    # One attempt, not a retry loop, and the restored copy is still served.
    assert upstream.requests == ["tripupdates"]
    assert realtime.fetched_at("SF", "tripupdates") == held
    assert realtime.db.payload_fetched_at("SF", "tripupdates") == held
    assert "500" in realtime.health()[0]["SF:tripupdates"].last_error


async def test_no_key_means_no_polling(tmp_path):
    realtime, factory, built = build(tmp_path, None, api_511_key="")
    persist(realtime.db, {"tripupdates": 5000})
    pollers = Pollers(realtime, client_factory=factory)
    await pollers.start()
    assert not pollers.polling and built == []
    # Still serves what was saved, however old.
    assert realtime.fetched_at("SF", "tripupdates") is not None
    await pollers.stop()


# MARK: - Fixtures mode


async def test_fixtures_mode_never_builds_the_client(tmp_path):
    realtime, factory, built = build(tmp_path, None, api_511_key="", fixtures=True)
    pollers = Pollers(realtime, client_factory=factory)
    await pollers.start()
    await until(lambda: all(realtime.fetched_at("SF", f) is not None for f in FEEDS))
    await pollers.stop()

    assert built == []
    assert realtime.db.calls() == []
    # Recorded data is never persisted, so it cannot pass for a fresh feed later.
    assert all(realtime.db.load_payload("SF", f) is None for f in FEEDS)
    assert realtime.arrivals(["SF:15621"], limit=6).platforms["SF:15621"]


async def test_fixtures_mode_ignores_a_key_too(tmp_path):
    realtime, factory, built = build(tmp_path, None, fixtures=True)
    pollers = Pollers(realtime, client_factory=factory)
    await pollers.start()
    await until(lambda: realtime.fetched_at("SF", "tripupdates") is not None)
    await pollers.stop()
    assert built == []


async def test_fixture_replay_cycles():
    replay = FixtureReplay(FIXTURES)
    sequence = [await replay.fetch("tripupdates", "SF") for _ in range(3)]
    one, two = (FIXTURES / "tripupdates-1.pb.gz").read_bytes(), (FIXTURES / "tripupdates-2.pb.gz").read_bytes()
    assert sequence == [one, two, one]
    assert await replay.fetch("servicealerts", "SF") == await replay.fetch("servicealerts", "SF")


# MARK: - The poller lock


async def test_a_second_process_does_not_poll_and_takes_over_when_the_first_stops(tmp_path):
    upstream = Upstream()
    first_rt, first_factory, _ = build(tmp_path, upstream)
    persist(first_rt.db, {"tripupdates": 10, "vehiclepositions": 10, "servicealerts": 10})
    second_rt, second_factory, second_built = build(tmp_path, upstream, db=Database(first_rt.db.path))

    first = Pollers(first_rt, client_factory=first_factory, owner="first", heartbeat_seconds=0.05)
    second = Pollers(second_rt, client_factory=second_factory, owner="second", heartbeat_seconds=0.05)
    await first.start()
    await second.start()
    assert first.polling and not second.polling
    assert second_built == []
    # The follower serves what the leader persisted.
    assert second_rt.fetched_at("SF", "tripupdates") == first_rt.fetched_at("SF", "tripupdates")

    # A newer payload persisted by the leader reaches the follower.
    newer = first_rt.fetched_at("SF", "vehiclepositions") + 5
    first_rt.db.save_payload("SF", "vehiclepositions", PAYLOADS["vehiclepositions"], newer)
    await until(lambda: second_rt.fetched_at("SF", "vehiclepositions") == newer)

    await first.stop()  # releases the lock
    await until(lambda: second.polling)
    assert len(second_built) == 1
    await second.stop()
    assert upstream.requests == []  # everything was fresh throughout


async def test_a_dead_holder_is_taken_over_after_it_goes_stale(tmp_path):
    upstream = Upstream()
    realtime, factory, _ = build(tmp_path, upstream)
    persist(realtime.db, {"tripupdates": 10, "vehiclepositions": 10, "servicealerts": 10})
    # A process that died without releasing, 30 s ago.
    assert realtime.db.acquire_lock("dead", time.time() - 30, stale_after=60).acquired

    pollers = Pollers(realtime, client_factory=factory, owner="new", heartbeat_seconds=0.05, lock_stale_seconds=31)
    await pollers.start()
    assert not pollers.polling
    await until(lambda: pollers.polling, timeout=5)
    await pollers.stop()


async def test_a_leader_that_loses_the_lock_stops_polling(tmp_path):
    upstream = Upstream()
    realtime, factory, _ = build(tmp_path, upstream)
    persist(realtime.db, {"tripupdates": 10, "vehiclepositions": 10, "servicealerts": 10})
    pollers = Pollers(realtime, client_factory=factory, owner="slow", heartbeat_seconds=0.05)
    await pollers.start()
    assert pollers.polling
    # Another process judged this one dead and took the lock.
    assert realtime.db.acquire_lock("usurper", time.time() + 3600, stale_after=0).acquired
    await until(lambda: not pollers.polling)
    assert pollers._feed_tasks == []
    await pollers.stop()
    # stop() released nothing that was not ours.
    assert not realtime.db.acquire_lock("third", time.time(), stale_after=60).acquired


@pytest.fixture(autouse=True)
def _quiet_logs(caplog):
    caplog.set_level("ERROR")
