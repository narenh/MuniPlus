"""Helpers shared by the realtime tests.

Settings are always built with ``_env_file=None`` and every relevant field
stated, so no test reads ``api/.env`` or picks up a real ``API_511_KEY`` from the
environment. The 511 client only ever sees ``httpx.MockTransport``.
"""

import asyncio
import gzip
from pathlib import Path

from app.settings import Settings

FIXTURES = Path(__file__).parents[1] / "fixtures" / "realtime"


def fixture_bytes(name: str) -> bytes:
    """A recorded feed, decompressed: what 511 itself sends."""
    return gzip.decompress((FIXTURES / name).read_bytes())


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        api_511_key="",
        fixtures=False,
        poll_arrivals_seconds=180,
        poll_vehicles_seconds=180,
        poll_alerts_seconds=1200,
        budget_per_hour=55,
        operators=["SF"],
        data_dir=tmp_path,
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


class FakeNetwork:
    """Just enough of track B's Network to satisfy ``NetworkView``."""

    def __init__(self, stations: dict[str, str] | None = None, headsigns: dict[tuple[str, int], str] | None = None):
        self.stations = stations or {}
        self.headsigns = headsigns or {}

    def station_of(self, platform_id: str) -> str | None:
        return self.stations.get(platform_id)

    def headsign(self, line_id: str, direction: int) -> str | None:
        return self.headsigns.get((line_id, direction))


async def until(condition, timeout: float = 3.0) -> None:
    """Wait for background tasks to reach a state, without a fixed sleep."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)
