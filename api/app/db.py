"""The server's own state, in SQLite at ``settings.db_path``.

Three things live here, all of them about talking to 511:

* the latest good raw payload per feed, so a restart serves data at once and
  does not spend a call on a feed it fetched a minute ago;
* a ledger of every 511 call, which is what the hourly budget is counted from.
  It is on disk rather than in memory because a restart must not refill the
  budget: a crash loop would otherwise be a stampede at 511;
* the poller lock, which is how "one worker polls" is enforced rather than
  hoped for.

Other tracks add tables by subclassing ``Base``; ``Database`` creates every table
registered on ``Base.metadata`` when it opens, so a module defining one has to be
imported first.

Every transaction is ``BEGIN IMMEDIATE``. The budget is check-then-insert, and
with SQLite's default deferred transactions two processes could both count 54,
both insert, and land on 56. IMMEDIATE takes the write lock before the count, so
the second waits (``busy_timeout``) and then counts 55.

Calls are synchronous. Each is a single-row read or write against a local file,
the largest being a ~1 MB payload every three minutes, which is not worth a
thread hop.
"""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Float, Integer, LargeBinary, String, create_engine, delete, event, func, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

BUSY_TIMEOUT_SECONDS = 10


class Base(DeclarativeBase):
    pass


class FeedPayload(Base):
    """The latest good payload of each feed. Only payloads that decoded are
    written, so a restore never starts from something the poller refused."""

    __tablename__ = "feed_payloads"

    operator: Mapped[str] = mapped_column(String, primary_key=True)
    feed: Mapped[str] = mapped_column(String, primary_key=True)
    payload: Mapped[bytes] = mapped_column(LargeBinary)
    fetched_at: Mapped[int] = mapped_column(Integer)
    """Epoch seconds, when 511 answered."""


class UpstreamCall(Base):
    """One row per 511 call, written *before* the call goes out and completed
    after. A call that never completes (a crash, a cancel mid-flight) still
    counts, because 511 may well have counted it."""

    __tablename__ = "upstream_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[float] = mapped_column(Float, index=True)
    operator: Mapped[str] = mapped_column(String)
    feed: Mapped[str] = mapped_column(String)
    status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """HTTP status; null while in flight or when no response came back."""
    rate_limit_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """511's ``RateLimit-Remaining`` header, where sent."""
    error: Mapped[str | None] = mapped_column(String, nullable=True)


class PollerLock(Base):
    __tablename__ = "poller_lock"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    owner: Mapped[str] = mapped_column(String)
    heartbeat: Mapped[float] = mapped_column(Float)


@dataclass(frozen=True, slots=True)
class StoredPayload:
    payload: bytes
    fetched_at: int


@dataclass(frozen=True, slots=True)
class LockState:
    acquired: bool
    owner: str
    heartbeat: float


LOCK_NAME = "pollers"


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": BUSY_TIMEOUT_SECONDS})

        @event.listens_for(self.engine, "connect")
        def _connect(dbapi_connection, _record):
            # Hand transaction control to the "begin" hook below; pysqlite's own
            # would issue a plain deferred BEGIN. This is SQLAlchemy's documented
            # recipe for SQLite transaction modes.
            dbapi_connection.isolation_level = None
            cursor = dbapi_connection.cursor()
            # WAL lets a second process read payloads while the poller writes.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        @event.listens_for(self.engine, "begin")
        def _begin(connection):
            connection.exec_driver_sql("BEGIN IMMEDIATE")

        Base.metadata.create_all(self.engine)

    def close(self) -> None:
        self.engine.dispose()

    # MARK: - Payloads

    def save_payload(self, operator: str, feed: str, payload: bytes, fetched_at: int) -> None:
        with Session(self.engine) as session, session.begin():
            session.merge(FeedPayload(operator=operator, feed=feed, payload=payload, fetched_at=fetched_at))

    def load_payload(self, operator: str, feed: str) -> StoredPayload | None:
        with Session(self.engine) as session, session.begin():
            row = session.get(FeedPayload, (operator, feed))
            return StoredPayload(row.payload, row.fetched_at) if row else None

    def payload_fetched_at(self, operator: str, feed: str) -> int | None:
        with Session(self.engine) as session, session.begin():
            return session.scalar(
                select(FeedPayload.fetched_at).where(FeedPayload.operator == operator, FeedPayload.feed == feed)
            )

    # MARK: - Ledger

    def reserve_call(self, operator: str, feed: str, at: float, per_hour: int) -> int | None:
        """Record a call about to be made and return its row id, or None (and
        record nothing) when ``per_hour`` calls already fall in the hour before
        ``at``. Count and insert are one transaction; see the module docstring."""
        with Session(self.engine) as session, session.begin():
            used = session.scalar(select(func.count()).select_from(UpstreamCall).where(UpstreamCall.at > at - 3600))
            if used >= per_hour:
                return None
            row = UpstreamCall(at=at, operator=operator, feed=feed)
            session.add(row)
            session.flush()
            return row.id

    def finish_call(self, call_id: int, *, status: int | None, rate_limit_remaining: int | None, error: str | None) -> None:
        with Session(self.engine) as session, session.begin():
            session.execute(
                update(UpstreamCall)
                .where(UpstreamCall.id == call_id)
                .values(status=status, rate_limit_remaining=rate_limit_remaining, error=error)
            )

    def calls_since(self, since: float) -> int:
        with Session(self.engine) as session, session.begin():
            return session.scalar(select(func.count()).select_from(UpstreamCall).where(UpstreamCall.at > since))

    def calls(self) -> list[UpstreamCall]:
        """Every ledger row, oldest first. For tests and inspection."""
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            return list(session.scalars(select(UpstreamCall).order_by(UpstreamCall.id)))

    def last_rate_limit_remaining(self) -> int | None:
        """From the most recent call that carried the header. A failed call often
        does not, and "unknown" would hide a perfectly good recent reading."""
        with Session(self.engine) as session, session.begin():
            return session.scalar(
                select(UpstreamCall.rate_limit_remaining)
                .where(UpstreamCall.rate_limit_remaining.is_not(None))
                .order_by(UpstreamCall.id.desc())
                .limit(1)
            )

    # MARK: - Poller lock

    def acquire_lock(self, owner: str, now: float, stale_after: float) -> LockState:
        """Take the lock if it is free, already ours, or its holder has not
        heartbeated for ``stale_after`` seconds (a process that died without
        releasing it)."""
        with Session(self.engine) as session, session.begin():
            row = session.get(PollerLock, LOCK_NAME)
            if row is None:
                session.add(PollerLock(name=LOCK_NAME, owner=owner, heartbeat=now))
                return LockState(True, owner, now)
            if row.owner == owner or row.heartbeat < now - stale_after:
                row.owner, row.heartbeat = owner, now
                return LockState(True, owner, now)
            return LockState(False, row.owner, row.heartbeat)

    def heartbeat(self, owner: str, now: float) -> bool:
        """Refresh the lock. False when it is no longer ours: another process
        judged us dead and took it, and we must stop polling."""
        with Session(self.engine) as session, session.begin():
            result = session.execute(
                update(PollerLock).where(PollerLock.name == LOCK_NAME, PollerLock.owner == owner).values(heartbeat=now)
            )
            return result.rowcount == 1

    def release_lock(self, owner: str) -> None:
        with Session(self.engine) as session, session.begin():
            session.execute(delete(PollerLock).where(PollerLock.name == LOCK_NAME, PollerLock.owner == owner))
