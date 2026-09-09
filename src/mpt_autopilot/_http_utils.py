"""
http_utils.py — shared HTTP infrastructure for MPT Autopilot.

Provides:
  - get_shared_client() / close_shared_client() — a single httpx.Client
    singleton reused across pilot, enricher, and notify to avoid repeated
    TLS handshakes and TCP warm-ups.
  - retrying_request() — parameterised HTTP request with retry/backoff,
    used by both pilot and enricher LLM paths.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any

import httpx

_logger = logging.getLogger(__name__)

# ── Shared client singleton ────────────────────────────────────────────────────

_shared_client: httpx.Client | None = None


def get_shared_client() -> httpx.Client:
    """
    Return the shared httpx.Client, creating it on first call.

    The timeout mirrors enricher/llm.py's previous behaviour: LLM_TIMEOUT
    seconds total, 10 seconds connect. Read after dotenv has been loaded.
    """
    global _shared_client
    if _shared_client is None:
        timeout = float(os.getenv("LLM_TIMEOUT", "60"))
        _shared_client = httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0))
    return _shared_client


def close_shared_client() -> None:
    """Close the shared client if it exists. Safe to call repeatedly."""
    global _shared_client
    if _shared_client is not None:
        _shared_client.close()
        _shared_client = None


# ── Retrying request helper ────────────────────────────────────────────────────

_RETRY_BACKOFF_BASE = 2.0  # seconds; doubles each attempt


def retrying_request(
    client: Any,
    method: str,
    url: str,
    *,
    max_retries: int = 3,
    backoff_base: float = _RETRY_BACKOFF_BASE,
    backoff_max: float = 20.0,
    transient_status: frozenset[int] | None = None,
    respect_retry_after: bool = False,
    log: Callable[..., None] | None = None,
    **kwargs: Any,
) -> Any:
    """
    Execute an HTTP request with exponential-backoff retry.

    Retries on:
      - Connection errors and timeouts (httpx.ConnectError, httpx.TimeoutException)
      - Status codes in transient_status (if provided)
      - 429 Too Many Requests (always retried; respects Retry-After when enabled)

    All other HTTP errors (4xx other than 429, 5xx when not in transient_status)
    are raised immediately via raise_for_status() or returned as-is.

    Args:
        client:            The httpx.Client to use (typically get_shared_client()).
        method:            HTTP method ("GET", "POST", …).
        url:               Full URL.
        max_retries:       Total attempts = 1 + max_retries.
        backoff_base:      Initial delay in seconds (doubles each attempt).
        backoff_max:       Ceiling for the computed delay.
        transient_status:  HTTP status codes to retry (None → empty set).
        respect_retry_after: honour the Retry-After header on 429 responses.
        log:               Optional logging callable (default: _logger.warning).
        **kwargs:          Passed to client.request() (json, headers, …).

    Returns:
        httpx.Response on success.

    Raises:
        httpx.HTTPStatusError: on non-retried HTTP errors.
        Last caught transport exception: when all retries are exhausted.
    """
    transient = transient_status or frozenset()
    _log = log or _logger.warning
    last_exc: Exception | None = None
    resp: Any = None  # initialized before loop so it's always in scope

    for attempt in range(max_retries + 1):
        try:
            resp = client.request(method, url, **kwargs)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            last_exc = exc
            if attempt >= max_retries:
                break
            delay = min(backoff_base * (2**attempt), backoff_max)
            _log(
                "%s — retrying in %.0fs (attempt %d/%d)",
                exc.__class__.__name__,
                delay,
                attempt + 1,
                max_retries,
            )
            time.sleep(delay)
            continue

        assert resp is not None  # noqa: S101 — caller's request() must return a response
        status = resp.status_code
        if status == 429:
            if attempt >= max_retries:
                raise RuntimeError(f"429 Too Many Requests — gave up after {max_retries} retries")
            delay = min(backoff_base * (2**attempt), backoff_max)
            if respect_retry_after:
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = min(float(retry_after), backoff_max)
            _log(
                "429 rate-limited — waiting %.0fs before retry %d/%d",
                delay,
                attempt + 1,
                max_retries,
            )
            time.sleep(delay)
            continue

        if status in transient:
            if attempt >= max_retries:
                body_preview = resp.text[:500].replace("\n", " ")
                _log("HTTP %d (final attempt) — body: %s", status, body_preview)
                last_exc = httpx.HTTPStatusError(
                    f"{status} {resp.reason_phrase} — gave up after {max_retries} retries",
                    request=resp.request,
                    response=resp,
                )
                break
            delay = min(backoff_base * (2**attempt), backoff_max)
            _log(
                "transient HTTP %d — retrying in %.0fs (attempt %d/%d)",
                status,
                delay,
                attempt + 1,
                max_retries,
            )
            time.sleep(delay)
            continue

        if not resp.is_success:
            resp.raise_for_status()

        return resp

    # All retries exhausted
    if resp is not None and last_exc is None:
        # Non-200 response that wasn't handled above
        raise httpx.HTTPStatusError(
            f"HTTP {resp.status_code} {resp.reason_phrase}",
            request=resp.request,
            response=resp,
        )
    raise last_exc or RuntimeError("HTTP request failed with unknown error")
