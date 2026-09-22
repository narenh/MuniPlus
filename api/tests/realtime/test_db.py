"""The SQLite state: the ledger's ceiling under real concurrency, payloads, the lock."""

import threading

from app.db import Database


def test_ledger_ceiling_holds_across_connections(tmp_path):
    # Separate Database objects in separate threads are separate SQLite
    # connections, which is what two processes look like to SQLite. With a
    # deferred transaction, count-then-insert races and overshoots.
    path = tmp_path / "muni.db"
    Database(path).close()
    granted: list[int] = []
    barrier = threading.Barrier(8)

    def worker():
        db = Database(path)
        barrier.wait()
        for _ in range(10):
            if db.reserve_call("SF", "tripupdates", 1000.0, per_hour=25) is not None:
                granted.append(1)
        db.close()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(granted) == 25
    assert Database(path).calls_since(0) == 25


def test_payload_round_trip(tmp_path):
    db = Database(tmp_path / "muni.db")
    assert db.load_payload("SF", "tripupdates") is None
    db.save_payload("SF", "tripupdates", b"one", 100)
    db.save_payload("SF", "tripupdates", b"two", 200)
    stored = db.load_payload("SF", "tripupdates")
    assert (stored.payload, stored.fetched_at) == (b"two", 200)
    assert db.payload_fetched_at("SF", "tripupdates") == 200


def test_lock(tmp_path):
    a, b = Database(tmp_path / "muni.db"), Database(tmp_path / "muni.db")
    assert a.acquire_lock("a", now=100, stale_after=60).acquired
    held = b.acquire_lock("b", now=110, stale_after=60)
    assert (held.acquired, held.owner, held.heartbeat) == (False, "a", 100)
    assert a.heartbeat("a", now=150)
    assert not b.acquire_lock("b", now=200, stale_after=60).acquired
    # a goes quiet for more than stale_after: b may take it, and a finds out.
    assert b.acquire_lock("b", now=211, stale_after=60).acquired
    assert not a.heartbeat("a", now=212)
    a.release_lock("a")  # not a's to release: a no-op
    assert not a.acquire_lock("a", now=213, stale_after=60).acquired
    b.release_lock("b")
    assert a.acquire_lock("a", now=214, stale_after=60).acquired
