"""
Regression tests for retrying_request() in _http_utils.py.

These tests verify that the shared retry helper preserves the retry semantics
from both pilot/llm.py and enricher/llm.py:
  - Connection/timeout errors are retried with exponential backoff
  - 429 is retried and respects Retry-After
  - Transient status codes (configurable) are retried
  - Non-retried HTTP errors propagate immediately
  - Successful responses are returned as-is
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import httpx
import pytest

from mpt_autopilot._http_utils import close_shared_client, retrying_request

# ── Helpers ────────────────────────────────────────────────────────────────────


class _FakeResponse:
    """Minimal httpx.Response stand-in for testing."""

    def __init__(self, status_code=200, text="", reason_phrase="OK", headers=None):
        self.status_code = status_code
        self.text = text
        self.reason_phrase = reason_phrase
        self.headers = headers or {}
        self.request = MagicMock()

    @property
    def is_success(self):
        return 200 <= self.status_code < 400

    def raise_for_status(self):
        if not self.is_success:
            raise RuntimeError(f"{self.status_code} {self.reason_phrase}")


class _FakeClient:
    """Records every call and returns pre-programmed responses."""

    def __init__(self, responses: list[_FakeResponse | Exception], calls=None):
        self._responses = responses
        self._index = 0
        self.calls = calls or []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        result = self._responses[self._index]
        self._index += 1
        if isinstance(result, Exception):
            raise result
        return result


def _make_client(responses, calls=None):
    return _FakeClient(responses, calls=calls)


# ── 200 OK ────────────────────────────────────────────────────────────────────


def test_success_on_first_try():
    client = _make_client([_FakeResponse(200, '{"ok": true}')])
    resp = retrying_request(client, "POST", "http://example.com", max_retries=3)
    assert resp.status_code == 200
    assert len(client.calls) == 1


# ── Retries on transient errors ───────────────────────────────────────────────


def test_retries_on_timeout_then_succeeds():
    responses = [
        httpx.TimeoutException("timeout"),
        _FakeResponse(200, '{"ok": true}'),
    ]
    client = _make_client(responses)
    resp = retrying_request(client, "POST", "http://example.com", max_retries=3)
    assert resp.status_code == 200
    assert len(client.calls) == 2


def test_retries_on_502_then_succeeds():
    responses = [
        _FakeResponse(502, "Bad Gateway"),
        _FakeResponse(200, '{"ok": true}'),
    ]
    client = _make_client(responses)
    resp = retrying_request(
        client,
        "GET",
        "http://example.com",
        max_retries=2,
        transient_status=frozenset({502}),
    )
    assert resp.status_code == 200
    assert len(client.calls) == 2


def test_exhausts_retries_on_timeout():
    responses = [httpx.TimeoutException("boom")] * 4  # 1 + 3 retries
    client = _make_client(responses)
    with pytest.raises(httpx.TimeoutException):
        retrying_request(client, "POST", "http://e.com", max_retries=3)
    assert len(client.calls) == 4


# ── Non-retried errors propagate immediately ──────────────────────────────────


def test_400_does_not_retry():
    client = _make_client([_FakeResponse(400, "Bad Request")])
    with pytest.raises(RuntimeError):
        retrying_request(client, "GET", "http://e.com", max_retries=3)
    assert len(client.calls) == 1


def test_401_does_not_retry():
    client = _make_client([_FakeResponse(401, "Unauthorized")])
    with pytest.raises(RuntimeError):
        retrying_request(client, "GET", "http://e.com", max_retries=3)
    assert len(client.calls) == 1


# ── Exponential backoff timing ────────────────────────────────────────────────


def test_backoff_doubles_on_each_attempt():
    delays = []
    real_sleep = time.sleep
    time.sleep = lambda s: delays.append(s)
    try:
        responses = [httpx.TimeoutException("boom")] * 3
        client = _make_client(responses)
        with pytest.raises(httpx.TimeoutException):
            retrying_request(
                client,
                "POST",
                "http://e.com",
                max_retries=2,
                backoff_base=2.0,
                backoff_max=60.0,
            )
    finally:
        time.sleep = real_sleep
    assert delays == [2.0, 4.0]


# ── Retry-After on 429 ────────────────────────────────────────────────────────


def test_respects_retry_after_header():
    responses = [
        _FakeResponse(429, headers={"Retry-After": "5"}),
        _FakeResponse(200, '{"ok": true}'),
    ]
    delays = []
    real_sleep = time.sleep
    time.sleep = lambda s: delays.append(s)
    try:
        client = _make_client(responses)
        resp = retrying_request(
            client,
            "POST",
            "http://e.com",
            max_retries=2,
            respect_retry_after=True,
            backoff_max=60.0,
        )
    finally:
        time.sleep = real_sleep
    assert resp.status_code == 200
    assert delays == [5.0]


# ── pytest teardown: make sure shared client isn't leaked ─────────────────────


@pytest.fixture(autouse=True)
def _reset_shared_client():
    close_shared_client()
    yield
    close_shared_client()
