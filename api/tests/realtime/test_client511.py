"""The 511 client: the budget ceiling, and what counts against it."""

import asyncio

import httpx
import pytest
from rt_support import make_settings

from app.db import Database
from app.upstream.client511 import BudgetExhausted, Client511, NoApiKey, RateLimited, UpstreamError

KEY = "test-key-not-real"


class Upstream:
    """A fake 511 that counts every request that reaches it."""

    def __init__(self, respond=None):
        self.requests: list[httpx.Request] = []
        self._respond = respond or (lambda request: httpx.Response(200, content=b"feed", headers={"RateLimit-Remaining": "42"}))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


class Clock:
    def __init__(self, t: float = 1_790_113_897.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def keyed(tmp_path):
    return make_settings(tmp_path, api_511_key=KEY, budget_per_hour=5)


async def test_burst_never_passes_the_ceiling_even_across_a_restart(keyed):
    upstream, clock = Upstream(), Clock()
    db = Database(keyed.db_path)
    client = Client511(keyed, db, transport=upstream.transport, clock=clock)

    # Eight concurrent calls against a budget of five.
    results = await asyncio.gather(*(client.fetch("tripupdates", "SF") for _ in range(8)), return_exceptions=True)
    assert sum(1 for r in results if r == b"feed") == 5
    assert sum(1 for r in results if isinstance(r, BudgetExhausted)) == 3
    assert len(upstream.requests) == 5
    assert db.calls_since(clock() - 3600) == 5
    await client.aclose()
    db.close()

    # A restart: a new database handle and a new client, on the same file.
    clock.t += 1800
    db = Database(keyed.db_path)
    restarted = Client511(keyed, db, transport=upstream.transport, clock=clock)
    for _ in range(3):
        with pytest.raises(BudgetExhausted):
            await restarted.fetch("vehiclepositions", "SF")
    assert len(upstream.requests) == 5
    assert db.calls_since(clock() - 3600) == 5  # refusals are not recorded

    # The window rolls: an hour after the burst, calls go out again.
    clock.t += 1801
    assert await restarted.fetch("servicealerts", "SF") == b"feed"
    assert len(upstream.requests) == 6
    await restarted.aclose()
    db.close()


async def test_a_429_counts_and_is_an_error(keyed):
    upstream = Upstream(lambda r: httpx.Response(429, headers={"RateLimit-Remaining": "0"}))
    db = Database(keyed.db_path)
    client = Client511(keyed, db, transport=upstream.transport)
    with pytest.raises(RateLimited) as caught:
        await client.fetch("tripupdates", "SF")
    assert caught.value.status == 429
    [call] = db.calls()
    assert (call.status, call.rate_limit_remaining, call.error) == (429, 0, "429 rate limited")
    await client.aclose()


async def test_failures_count_too(keyed):
    def respond(request):
        raise httpx.ReadTimeout("timed out", request=request)

    upstream = Upstream(respond)
    db = Database(keyed.db_path)
    client = Client511(keyed, db, transport=upstream.transport)
    for _ in range(5):
        with pytest.raises(UpstreamError) as caught:
            await client.fetch("tripupdates", "SF")
        assert KEY not in str(caught.value)
    with pytest.raises(BudgetExhausted):
        await client.fetch("tripupdates", "SF")
    assert len(upstream.requests) == 5
    assert all(c.status is None and c.error.startswith("timeout") for c in db.calls())
    await client.aclose()


async def test_server_errors_are_recorded(keyed):
    db = Database(keyed.db_path)
    client = Client511(keyed, db, transport=Upstream(lambda r: httpx.Response(503)).transport)
    with pytest.raises(UpstreamError) as caught:
        await client.fetch("servicealerts", "SF")
    assert caught.value.status == 503
    assert [(c.feed, c.status, c.error) for c in db.calls()] == [("servicealerts", 503, "HTTP 503")]
    await client.aclose()


async def test_request_shape_and_rate_limit_header(keyed):
    upstream = Upstream()
    db = Database(keyed.db_path)
    client = Client511(keyed, db, transport=upstream.transport)
    await client.fetch("vehiclepositions", "SF")
    [request] = upstream.requests
    assert request.url.host == "api.511.org"
    assert request.url.path == "/transit/vehiclepositions"
    assert dict(request.url.params) == {"api_key": KEY, "agency": "SF"}
    assert db.last_rate_limit_remaining() == 42
    await client.aclose()


async def test_the_key_never_reaches_the_logs(keyed, caplog):
    caplog.set_level("DEBUG")
    db = Database(keyed.db_path)
    client = Client511(keyed, db, transport=Upstream().transport)
    await client.fetch("tripupdates", "SF")
    assert "api.511.org/transit/tripupdates" in caplog.text  # httpx did log the request
    assert KEY not in caplog.text
    await client.aclose()


async def test_no_key_means_no_calls(tmp_path):
    settings = make_settings(tmp_path, api_511_key="")
    upstream = Upstream()
    db = Database(settings.db_path)
    client = Client511(settings, db, transport=upstream.transport)
    with pytest.raises(NoApiKey):
        await client.fetch("tripupdates", "SF")
    assert upstream.requests == []
    assert db.calls() == []
    await client.aclose()


async def test_a_cancelled_call_still_counts(keyed):
    started = asyncio.Event()

    async def slow(request):
        started.set()
        await asyncio.sleep(60)
        return httpx.Response(200)

    class SlowTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return await slow(request)

    db = Database(keyed.db_path)
    client = Client511(keyed, db, transport=SlowTransport())
    task = asyncio.create_task(client.fetch("tripupdates", "SF"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # 511 may have counted it, so we do.
    assert db.calls_since(0) == 1
    await client.aclose()
