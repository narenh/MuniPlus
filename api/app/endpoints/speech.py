"""``GET /api/v1/speech?stop=SF:13853&limit=3``: the next arrivals at one stop as one sentence.

For voice assistants: a Shortcut that fetches this and hands the text to Siri
reads "There's a K Ingleside in 2 minutes, an M Ocean View in 5 minutes, and a 1X
California Express in 7 minutes." with no JSON or date handling of its own.

Plain text, errors included, because whatever comes back is read aloud. A JSON
Problem spoken by Siri is noise; its message is a sentence already.
"""

import re

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from app.models.api import Arrival
from app.models.ids import operator_of
from app.realtime.http import NO_STORE, BadRequest, Unavailable, parse_ids, parse_int, realtime_of

from .arrivals import MAX_LIMIT

router = APIRouter(prefix="/api/v1", tags=["realtime"])

DEFAULT_LIMIT = 3
"""Arrivals spoken. Three is about what a listener keeps hold of."""
MAX_SPOKEN = 10

_LEADING_NUMBER = re.compile(r"\d+")


@router.get("/speech", response_class=PlainTextResponse)
def speech(request: Request, stop: str | None = None, limit: str | None = None) -> PlainTextResponse:
    try:
        if stop is not None and "," in stop:
            raise BadRequest("bad-request", "stop takes one stop id, like SF:13853.")
        (stop_id,) = parse_ids(stop, param="stop", kind="platform", required=True, max_count=1)
        count = parse_int(limit, param="limit", default=DEFAULT_LIMIT, maximum=MAX_SPOKEN)
        realtime = realtime_of(request)
    except BadRequest as bad:
        return _text(bad.message, 400)
    except Unavailable:
        return _text("Predictions aren't available right now.", 503)

    # Asked for in full, because arrivals that terminate here are dropped below
    # and would otherwise leave fewer than ``count``.
    answer = realtime.arrivals([stop_id], MAX_LIMIT)
    if answer.fetched_at is None:
        return _text("Predictions aren't available right now.", 503)
    # One entry, keyed by the platform the stop belongs to rather than the stop.
    (arrivals,) = answer.platforms.values()
    boarding = [a for a in arrivals if not a.terminates][:count]
    return _text(sentence(boarding, realtime.now(operator_of(stop_id)), _line_names(request)))


def sentence(arrivals: list[Arrival], now: int, names: dict[str, tuple[str, str]]) -> str:
    """One sentence, a clause per line in the order its first vehicle comes:
    "There's a 1 California in 6, 12 and 18 minutes, and an M Ocean View now."
    ``names`` maps a line id to its short name and full name."""
    if not arrivals:
        return "Nothing's due here right now."
    minutes: dict[str, list[int]] = {}
    for arrival in arrivals:
        minutes.setdefault(arrival.line, []).append(max(0, (arrival.time - now) // 60))
    clauses = []
    for line, mins in minutes.items():
        short, name = names.get(line) or (line.split(":", 1)[1],) * 2
        clauses.append(f"{article(short)} {name} {_when(mins)}")
    return f"There's {_join(clauses)}."


def article(short_name: str) -> str:
    """The article for how a line's short name is said: an M, an 8, an 18, a 1X."""
    number = _LEADING_NUMBER.match(short_name)
    if number:
        n = number.group(0)
        return "an" if n.startswith("8") or n in ("11", "18") else "a"
    # Letters said with a leading vowel sound: "eff", "em", "en", ...
    return "an" if short_name[:1].upper() in set("AEFHILMNORSX") else "a"


def _when(minutes: list[int]) -> str:
    """When, in words: "now", "in 1 minute", "now and in 6 minutes", "in 6, 12 and 18 minutes"."""
    due = [m for m in minutes if m > 0]
    later = ""
    if due:
        unit = "minute" if due == [1] else "minutes"
        later = f"in {_join([str(m) for m in due], serial=False)} {unit}"
    if len(due) == len(minutes):
        return later
    return f"now and {later}" if later else "now"


def _join(items: list[str], *, serial: bool = True) -> str:
    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + ("," if serial else "") + f" and {items[-1]}"


def _line_names(request: Request) -> dict[str, tuple[str, str]]:
    network = getattr(request.app.state, "network", None)
    if network is None:
        return {}
    return {line.id: (line.short_name, line.name) for line in network.lines().lines}


def _text(body: str, status: int = 200) -> PlainTextResponse:
    return PlainTextResponse(body, status_code=status, headers=NO_STORE)
