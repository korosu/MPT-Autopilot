"""
web/status.py — one question: is the MoneyPrinterTurbo API answering?

The only stage that talks to MPT is `batch`, so the URL to probe comes from
`batch.api_url` — the same address a real render would use, which means the
indicator can never report about a server the pipeline would not use either.

The endpoint that decides is `GET {api_url}/api/v1/tasks` — the one batch
actually calls (batch/api.py:health_check), validated with the same body shape:
a dict with a list at ``data`` or a task list at ``data.tasks``. `/ping` is the
secondary path: newer MPT ships it unauthenticated, but the probe must not
depend on it, because a deployment without that route (the reference user's
VPS runs MPT 1.3.3, where /ping answers 404 while /api/v1/tasks answers 200)
would be reported down/amber while every batch run succeeds. A missing /ping
says nothing about the server that owns the port; an API that answers with a
task list does.

Three states, not two, because "something answered" and "MoneyPrinterTurbo
answered" are different facts:

- up         — /api/v1/tasks answered with a task list, or /ping answered 200.
- reachable  — something answered, but neither endpoint proved MoneyPrinterTurbo
                (a 404/401 port squatter or a gateway error page), so a render
                would fail. Reporting this as "up" reads as "everything works",
                which is the one thing it must not say.
- down       — nothing answered at all.

`via` records which endpoint proved "up" ("tasks" or "ping"), so a surprising
verdict can be traced without re-running the probe by hand.

Pure httpx + stdlib on purpose: no Flask here, so tests need no [web] extra.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

# Short on purpose: the browser polls every ~15 s, so a hung server must not
# hold a request thread hostage. MPT answers /api/v1/tasks in tens of ms when
# up (a task listing is a dict lookup over in-memory state).
PROBE_TIMEOUT_SECONDS = 2.0

TASKS_PATH = "/api/v1/tasks"
PING_PATH = "/ping"


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
    # "tasks" or "ping" — which endpoint proved `up`; None otherwise.
    via: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "configured": self.configured,
            "up": self.up,
            "reachable": self.reachable,
            "status_code": self.status_code,
            "latency_ms": self.latency_ms,
            "api_url": self.api_url,
            "host": self.host,
            "via": self.via,
        }


@dataclass(frozen=True)
class _Answer:
    """One HTTP GET: a status code, or None when the transport itself failed."""

    status_code: int | None
    body: object = None

    @property
    def answered(self) -> bool:
        return self.status_code is not None


def _get(client: httpx.Client, url: str) -> _Answer:
    """GET `url`, keeping transport failures and non-200 answers distinguishable."""
    try:
        response = client.get(url)
    except httpx.HTTPError:
        return _Answer(None)
    try:
        body: object = response.json()
    except ValueError:
        body = None
    return _Answer(response.status_code, body)


def _looks_like_task_list(body: object) -> bool:
    """
    The body shape batch.api.health_check accepts (batch/api.py:126-138):
    a dict with a list at ``data`` (validation errors) or a task list at
    ``data.tasks``. A 200 carrying anything else is a gateway error page or a
    different API on the same port — not proof of MoneyPrinterTurbo.
    """
    if not isinstance(body, dict):
        return False
    data = body.get("data")
    if isinstance(data, list):
        return True
    return isinstance(data, dict) and isinstance(data.get("tasks"), list)


def probe(
    api_url: str | None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    transport: httpx.BaseTransport | None = None,
) -> MptStatus:
    """
    Probe MPT once. `transport` exists for tests (httpx.MockTransport); None
    means a real network call.
    """
    if not api_url:
        return MptStatus(False, False, False, None, None, None, None)

    # batch's own URL convention (batch/settings.py rstrips the trailing
    # slash before appending /api/v1/...); both endpoints get the same treatment.
    base = api_url.rstrip("/")
    netloc = urlsplit(api_url).netloc or None

    started = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            tasks = _get(client, f"{base}{TASKS_PATH}")
            if tasks.status_code == 200 and _looks_like_task_list(tasks.body):
                return _verdict(True, True, "tasks", 200, started, api_url, netloc)
            ping = _get(client, f"{base}{PING_PATH}")
    except httpx.HTTPError:
        # Neither endpoint even connected — the client-level failure already
        # produced two _Answer(None)s above; reaching here means the Client
        # itself could not be built/entered.
        return MptStatus(True, False, False, None, None, api_url, netloc)

    latency_ms = int((time.perf_counter() - started) * 1000)
    if ping.status_code == 200:
        return _verdict(True, True, "ping", 200, started, api_url, netloc)
    if not tasks.answered and not ping.answered:
        return MptStatus(True, False, False, None, None, api_url, netloc)
    # Something owns the port, but it is not MoneyPrinterTurbo's API. The
    # endpoint batch would use is the more informative code to report.
    code = tasks.status_code if tasks.answered else ping.status_code
    return MptStatus(True, False, True, code, latency_ms, api_url, netloc)


def _verdict(
    up: bool,
    reachable: bool,
    via: str,
    status_code: int,
    started: float,
    api_url: str,
    netloc: str | None,
) -> MptStatus:
    latency_ms = int((time.perf_counter() - started) * 1000)
    return MptStatus(True, up, reachable, status_code, latency_ms, api_url, netloc, via)
