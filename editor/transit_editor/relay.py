"""The editor's one upstream: the Canopy Transit Relay's copies of 511's static files.

The relay holds the 511 key and caches each file at the edge for a day, so the
editor needs no key, no budget and no ledger of its own. A snapshot refresh asks
for two files:

* ``GET {RELAY_URL}/static/datafeeds?operator=SF``: 511's GTFS zip (~8 MB);
* ``GET {RELAY_URL}/static/lines?operator=SF``: 511's line list as JSON, for
  each line's ``TransportMode``.

Both are 511's bytes unchanged. The relay answers 503 with ``{error, message}``
when it has no copy and 511 will not give it one.
"""

from typing import Literal

import httpx

Static = Literal["datafeeds", "lines"]

TIMEOUT_SECONDS = 120.0
"""The zip is about 8 MB. A refresh is started by hand from the editor, where
waiting is fine; a relay cache miss also waits on 511."""


class RelayError(Exception):
    """The relay did not produce the file."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class Relay:
    def __init__(self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(transport=transport, timeout=TIMEOUT_SECONDS, follow_redirects=True)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def fetch_static(self, file: Static, operator: str) -> bytes:
        """The raw body of one of 511's static files for ``operator``."""
        url = f"{self.base_url}/static/{file}"
        try:
            response = await self._http.get(url, params={"operator": operator})
        except httpx.TimeoutException:
            raise RelayError(f"the relay did not answer for {file} within {TIMEOUT_SECONDS:g}s") from None
        except httpx.HTTPError as exc:
            raise RelayError(f"could not reach the relay for {file} ({type(exc).__name__})") from None
        if not 200 <= response.status_code < 300:
            raise RelayError(f"the relay answered {response.status_code} for {file}: {_message(response)}",
                             status=response.status_code)
        return response.content


def _message(response: httpx.Response) -> str:
    """The relay's own ``message``, or the start of whatever else came back."""
    try:
        body = response.json()
        if isinstance(body, dict) and isinstance(body.get("message"), str):
            return body["message"]
    except ValueError:
        pass
    return response.text[:200].strip() or "no body"
