"""
web/status.py — one question: is the MoneyPrinterTurbo API answering?

The only stage that talks to MPT is `batch`, so the URL to probe comes from
`batch.api_url` — the same address a real render would use, which means the
indicator can never report about a server the pipeline would not use either.

MPT ships a healthcheck: `GET /ping` answers "pong" (app/controllers/ping.py,
upstream and in local installs alike), and it is not behind the x-api-key
protection (which covers /api/v1 and /tasks), so no key is sent.

Three states, not two, because "something answered" and "MoneyPrinterTurbo
answered" are different facts:

- up         — /ping answered 200. This is MPT, and it is healthy.
- reachable  — something answered, but not with 200 /ping. The port is taken
               by a server that is not MoneyPrinterTurbo (a hardened fork
               guarding /ping, or a different service on the same port), so a
               render would fail. Reporting this as "up" reads as
               "everything works", which is the one thing it must not say.
- down       — nothing answered at all.

Pure httpx + stdlib on purpose: no Flask here, so tests need no [web] extra.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

# Short on purpose: the browser polls every ~15 s, so a hung server must not
# hold a request thread hostage. MPT answers /ping in single-digit ms when up.
PROBE_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class MptStatus:
    """Everything the status dot knows, JSON-ready."""

    configured: bool
    up: bool
    reachable: bool
    status_code: int | None
    latency_ms: int | None
    api_url: str | None
    host: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "configured": self.configured,
            "up": self.up,
            "reachable": self.reachable,
            "status_code": self.status_code,
            "latency_ms": self.latency_ms,
            "api_url": self.api_url,
            "host": self.host,
        }


def probe(
    api_url: str | None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    transport: httpx.BaseTransport | None = None,
) -> MptStatus:
    """
    Ping MPT once. `transport` exists for tests (httpx.MockTransport); None
    means a real network call.
    """
    if not api_url:
        return MptStatus(False, False, False, None, None, None, None)

    # batch's own URL convention (batch/settings.py rstrips the trailing
    # slash before appending /api/v1/...); /ping gets the same treatment.
    ping_url = f"{api_url.rstrip('/')}/ping"
    netloc = urlsplit(api_url).netloc or None

    started = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            response = client.get(ping_url)
    except httpx.HTTPError:
        return MptStatus(True, False, False, None, None, api_url, netloc)

    latency_ms = int((time.perf_counter() - started) * 1000)
    if response.status_code != 200:
        # Something answered, but it is not MPT's healthcheck — the honest
        # verdict is "reachable, wrong server", not "up".
        return MptStatus(True, False, True, response.status_code, latency_ms, api_url, netloc)
    return MptStatus(True, True, True, response.status_code, latency_ms, api_url, netloc)
