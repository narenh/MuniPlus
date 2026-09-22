"""One asyncio task per feed per operator, keeping ``Realtime`` fresh.

Startup order is by cost, the same as the Worker's (memory, then storage, then
511): each feed's persisted payload is loaded first, and a feed is fetched only
once that copy is older than its interval. A deploy or crash-restart therefore
spends nothing on feeds fetched a minute ago. The Worker learned this the hard
way: without it, every cold start was an upstream call.

Only one process polls. The poller lock in SQLite is taken with a heartbeat; a
second process (an overlapping deploy, or someone starting two uvicorn workers)
cannot take it, so it does not poll: it serves the persisted payloads, picks up
newer ones as the poller writes them, and takes over only once the holder has
missed its heartbeat for ``LOCK_STALE_SECONDS``. Two pollers would each spend
the budget and together exceed 511's 60/hr.

Modes:

* ``FIXTURES=1``: replay ``tests/fixtures/realtime`` on the same intervals. The
  511 client is never built, nothing is persisted (so recorded data can never be
  mistaken for a fresh feed later), and no lock is taken.
* no ``API_511_KEY``: no polling at all; persisted payloads only.
* otherwise: poll 511 through ``Client511``.
"""

import asyncio
import contextlib
import logging
import os
import socket
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from app.upstream.client511 import Client511, UpstreamError
from app.upstream.gtfsrt import FEEDS, Feed

from .state import Realtime

log = logging.getLogger(__name__)

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "realtime"
HEARTBEAT_SECONDS = 15.0
LOCK_STALE_SECONDS = 60.0
"""Four missed heartbeats. Long enough that a slow event loop (a 1 MB decode, a
10 s upstream timeout) never looks dead; short enough that a replacement process
starts polling within a minute of the old one dying."""


class Source(Protocol):
    async def fetch(self, feed: Feed, operator: str) -> bytes: ...


class FixtureReplay:
    """Serves the recorded feeds in turn: ``tripupdates-1``, ``-2``, ``-1``...
    so a local run sees the feed change the way it does live."""

    def __init__(self, directory: Path = FIXTURES_DIR):
        self._files = {feed: sorted(directory.glob(f"{feed}*.pb.gz")) for feed in FEEDS}
        self._next = {feed: 0 for feed in FEEDS}

    async def fetch(self, feed: Feed, operator: str) -> bytes:
        files = self._files[feed]
        # The recordings are SF's; replaying them as another operator would
        # qualify SF stop ids with the wrong operator.
        if operator != "SF" or not files:
            raise UpstreamError(f"no fixture for {operator} {feed}")
        path = files[self._next[feed] % len(files)]
        self._next[feed] += 1
        return path.read_bytes()


class Pollers:
    def __init__(
        self,
        realtime: Realtime,
        *,
        client_factory: Callable[[], Client511] | None = None,
        fixtures_dir: Path = FIXTURES_DIR,
        owner: str | None = None,
        clock: Callable[[], float] = time.time,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        lock_stale_seconds: float = LOCK_STALE_SECONDS,
    ):
        self.realtime = realtime
        self.settings = realtime.settings
        self.db = realtime.db
        self._client_factory = client_factory or (lambda: Client511(self.settings, self.db))
        self._fixtures_dir = fixtures_dir
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._clock = clock
        self._heartbeat_seconds = heartbeat_seconds
        self._lock_stale_seconds = lock_stale_seconds
        self._client: Client511 | None = None
        self._feed_tasks: list[asyncio.Task] = []
        self._control: asyncio.Task | None = None
        self.polling = False
        """True while this process holds the lock (or replays fixtures)."""
        self.next_poll: dict[tuple[str, Feed], float] = {}
        """When each feed is next due, as its task goes to sleep. For health and tests."""

    # MARK: - Lifecycle

    async def start(self) -> None:
        if self.settings.fixtures:
            log.info("FIXTURES=1: replaying %s; 511 is never called", self._fixtures_dir)
            self._spawn_feeds(FixtureReplay(self._fixtures_dir), persist=False)
            return

        self.restore()
        if not self.settings.api_511_key:
            log.warning("API_511_KEY is empty: not polling 511; serving persisted payloads only")
            return

        lock = self.db.acquire_lock(self.owner, self._clock(), self._lock_stale_seconds)
        if lock.acquired:
            self._lead()
        else:
            log.warning(
                "poller lock is held by %s (heartbeat %.0fs ago): this process will not poll; "
                "serving persisted payloads and taking over if the holder stops heartbeating",
                lock.owner, self._clock() - lock.heartbeat,
            )
        self._control = asyncio.create_task(self._control_loop(), name="realtime-poller-lock")

    async def stop(self) -> None:
        tasks = [*self._feed_tasks, *([self._control] if self._control else [])]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._feed_tasks, self._control = [], None
        self.next_poll = {}
        if self.polling and not self.settings.fixtures:
            # Released rather than left to go stale, so the next deploy starts
            # polling at once instead of waiting out LOCK_STALE_SECONDS.
            self.db.release_lock(self.owner)
        self.polling = False
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # MARK: - Restore

    def restore(self) -> None:
        """Load every persisted payload newer than what is held."""
        for operator in self.settings.operators:
            for feed in FEEDS:
                held = self.realtime.fetched_at(operator, feed)
                stored_at = self.db.payload_fetched_at(operator, feed)
                if stored_at is None or (held is not None and stored_at <= held):
                    continue
                stored = self.db.load_payload(operator, feed)
                if stored is not None:
                    self.realtime.ingest(operator, feed, stored.payload, stored.fetched_at)

    # MARK: - Leading and following

    def _lead(self) -> None:
        log.info("took the poller lock as %s", self.owner)
        if self._client is None:
            self._client = self._client_factory()
        self._spawn_feeds(self._client, persist=True)

    def _spawn_feeds(self, source: Source, *, persist: bool) -> None:
        self.polling = True
        self._feed_tasks = [
            asyncio.create_task(self._poll_loop(operator, feed, source, persist), name=f"poll-{operator}-{feed}")
            for operator in self.settings.operators
            for feed in FEEDS
        ]

    async def _stop_feeds(self) -> None:
        for task in self._feed_tasks:
            task.cancel()
        for task in self._feed_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._feed_tasks = []
        self.next_poll = {}
        self.polling = False

    async def _control_loop(self) -> None:
        """Leader: heartbeat, and stand down if the lock was taken. Follower:
        pick up what the leader persists, and take over if it goes quiet."""
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            try:
                if self.polling:
                    if not self.db.heartbeat(self.owner, self._clock()):
                        log.error("lost the poller lock (another process judged this one dead): no longer polling")
                        await self._stop_feeds()
                else:
                    self.restore()
                    if self.db.acquire_lock(self.owner, self._clock(), self._lock_stale_seconds).acquired:
                        self.restore()
                        self._lead()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("poller lock upkeep failed; retrying")

    # MARK: - Polling

    async def _poll_loop(self, operator: str, feed: Feed, source: Source, persist: bool) -> None:
        interval = self.realtime.interval(feed)
        last = self.realtime.fetched_at(operator, feed)
        # Scheduled from the last attempt, not the last success: a failing feed
        # must wait out its interval like a working one, or a 511 outage becomes
        # a retry loop that spends the whole budget in a minute.
        next_at = last + interval if last is not None else 0.0
        while True:
            self.next_poll[(operator, feed)] = next_at
            delay = next_at - self._clock()
            if delay > 0:
                await asyncio.sleep(delay)
            started = self._clock()
            await self.poll_once(operator, feed, source, persist=persist)
            next_at = started + interval

    async def poll_once(self, operator: str, feed: Feed, source: Source, *, persist: bool) -> bool:
        try:
            payload = await source.fetch(feed, operator)
        except asyncio.CancelledError:
            raise
        except UpstreamError as error:
            self.realtime.record_error(operator, feed, str(error))
            return False
        except Exception as error:
            log.exception("polling %s %s failed", operator, feed)
            self.realtime.record_error(operator, feed, f"{type(error).__name__}: {error}")
            return False
        fetched_at = int(self._clock())
        if not self.realtime.ingest(operator, feed, payload, fetched_at):
            return False
        if persist:
            self.db.save_payload(operator, feed, payload, fetched_at)
        return True
