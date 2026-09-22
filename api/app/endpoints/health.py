"""``GET /health``: whether the data and the feeds are there, and the 511 budget.

Reads only what is already in memory and the call ledger, never 511 and never
git, so checking it costs nothing and cannot itself fail the thing it reports on.
"""

from fastapi import APIRouter, Request, Response

from ..models.api import BudgetHealth, Health

router = APIRouter(tags=["health"])

STALE_AFTER_INTERVALS = 3
"""A feed is stale once it has missed this many polls. One missed poll is a blip
(511 timed out); three is something to look at."""


@router.get("/health", response_model=Health)
def health(request: Request, response: Response) -> Health:
    state = request.app.state
    settings = state.settings
    network = getattr(state, "network", None)
    realtime = getattr(state, "realtime", None)
    problems: list[str] = []

    if network is None:
        problems.append(f"no station data: {getattr(state, 'data_error', None) or 'not loaded'}")

    if realtime is not None:
        feeds, budget = realtime.health()
    else:
        feeds, budget = {}, BudgetHealth(per_hour=settings.budget_per_hour, used_last_hour=0, last_rate_limit_remaining=None)

    if settings.polling_enabled:
        for name, feed in feeds.items():
            if feed.fetched_at is None:
                problems.append(f"{name} has never been fetched" + (f": {feed.last_error}" if feed.last_error else ""))
            elif feed.age_seconds > STALE_AFTER_INTERVALS * feed.interval_seconds:
                problems.append(f"{name} is {feed.age_seconds}s old" + (f": {feed.last_error}" if feed.last_error else ""))
    else:
        problems.append("realtime is off: no API_511_KEY and not FIXTURES")

    response.headers["Cache-Control"] = "no-store"
    return Health(
        ok=not problems,
        version=network.version if network else None,
        fixtures=settings.fixtures,
        feeds=feeds,
        budget=budget,
        problems=problems,
    )
