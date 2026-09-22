"""The only code that calls 511.

The dev key allows 60 calls an hour, shared by every feed, so one feed
overspending starves the other two. The ceiling (``settings.budget_per_hour``,
55) is enforced here, before a request exists, and counted from the ledger in
SQLite rather than from memory: a restarted process sees every call its
predecessor made in the last hour, so a crash loop cannot spend the key.

Every call is written to the ledger *before* it goes out. A timeout, a network
error, a 429 or a call cancelled mid-flight all still count, because 511 may have
counted them, and a budget that only counted successes would let a flapping
upstream be retried straight through the ceiling.
"""

import logging
import time
from collections.abc import Callable

import httpx

from app.db import Database
from app.settings import Settings

from .gtfsrt import Feed

BASE_URL = "https://api.511.org/transit"
TIMEOUT_SECONDS = 10.0
"""The Worker's value, which it has run in production. Its cold fetch of a 683 KB
TripUpdates took 877 ms end to end, so ten seconds is generous for a slow 511
while still ending a hung connection long before the next poll is due."""


class UpstreamError(Exception):
    """A 511 call that did not produce a payload."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class RateLimited(UpstreamError):
    """511 said 429. The call still counts against our budget."""


class BudgetExhausted(UpstreamError):
    """Refused locally: no request was made and nothing was recorded."""


class NoApiKey(UpstreamError):
    """No ``API_511_KEY``: the client makes no calls at all."""


class Client511:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._key = settings.api_511_key
        self._per_hour = settings.budget_per_hour
        self._db = db
        self._clock = clock
        self._http = httpx.AsyncClient(transport=transport, timeout=TIMEOUT_SECONDS)
        if self._key:
            _redact_in_httpx_logs(self._key)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def fetch(self, feed: Feed, operator: str) -> bytes:
        """The raw body of ``/transit/<feed>`` for one operator."""
        if not self._key:
            raise NoApiKey("API_511_KEY is not set")

        call_id = self._db.reserve_call(operator, feed, self._clock(), self._per_hour)
        if call_id is None:
            raise BudgetExhausted(f"{self._per_hour} calls to 511 in the last hour; not calling")

        status: int | None = None
        remaining: int | None = None
        error: str | None = None
        try:
            response = await self._http.get(
                f"{BASE_URL}/{feed}", params={"api_key": self._key, "agency": operator}
            )
            status = response.status_code
            remaining = _int_header(response.headers.get("RateLimit-Remaining"))
            if status == 429:
                error = "429 rate limited"
                raise RateLimited(f"511 rate limited {feed} (RateLimit-Remaining {remaining})", status=429)
            if not 200 <= status < 300:
                error = f"HTTP {status}"
                raise UpstreamError(f"511 answered {status} for {feed}", status=status)
            return response.content
        except httpx.TimeoutException as exc:
            error = f"timeout after {TIMEOUT_SECONDS:g}s ({type(exc).__name__})"
            raise UpstreamError(f"511 {feed}: {error}") from None
        except httpx.HTTPError as exc:
            # httpx exceptions carry the request, whose URL holds the key. Only
            # the class name and a scrubbed message ever leave this function.
            error = f"{type(exc).__name__}: {self._scrub(str(exc))}"
            raise UpstreamError(f"511 {feed}: {error}") from None
        finally:
            self._db.finish_call(call_id, status=status, rate_limit_remaining=remaining, error=error)

    def _scrub(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text


class _RedactKey(logging.Filter):
    """httpx logs every request at INFO with its full URL, and 511 only takes the
    key as a query parameter (``HTTP Request: GET ...?api_key=<key>&agency=SF``,
    checked with httpx 0.28). Any INFO-level logging config would put the key in
    the deploy logs, so it is replaced before the record is formatted."""

    def __init__(self, key: str):
        super().__init__()
        self.key = key

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self.key in message:
            record.msg, record.args = message.replace(self.key, "***"), ()
        return True


def _redact_in_httpx_logs(key: str) -> None:
    httpx_log = logging.getLogger("httpx")
    if not any(isinstance(f, _RedactKey) and f.key == key for f in httpx_log.filters):
        httpx_log.addFilter(_RedactKey(key))


def _int_header(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
