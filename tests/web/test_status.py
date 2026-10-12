"""
Tests for the MoneyPrinterTurbo health probe.

httpx.MockTransport stands in for the network, so nothing here leaves the
process — including the ConnectError case, which the transport raises instead
of the OS.
"""

from __future__ import annotations

import httpx

from mpt_autopilot.web.status import MptStatus, probe


def test_a_missing_batch_section_reports_not_configured():
    status = probe(None)
    assert status == MptStatus(False, False, None, None, None, None)
    assert status.as_dict() == {
        "configured": False,
        "up": False,
        "status_code": None,
        "latency_ms": None,
        "api_url": None,
        "host": None,
    }


def test_a_pong_answer_means_up():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, text="pong")

    status = probe("http://127.0.0.1:8080", transport=httpx.MockTransport(handler))
    assert (status.up, status.status_code, status.host) == (True, 200, "127.0.0.1:8080")
    assert status.latency_ms is not None and status.latency_ms >= 0
    assert seen["url"] == "http://127.0.0.1:8080/ping"


def test_a_trailing_slash_in_api_url_still_hits_ping():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://mpt.local:8080/ping"
        return httpx.Response(200)

    status = probe("http://mpt.local:8080/", transport=httpx.MockTransport(handler))
    assert status.up is True


def test_any_http_answer_counts_as_up():
    """A 401/404 still proves the server exists — only a refused connection is down."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    status = probe("http://127.0.0.1:8080", transport=httpx.MockTransport(handler))
    assert (status.up, status.status_code) == (True, 401)


def test_a_connection_refusal_means_down():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    status = probe("http://127.0.0.1:9999", transport=httpx.MockTransport(handler))
    assert (status.up, status.status_code, status.latency_ms) == (False, None, None)
    assert status.host == "127.0.0.1:9999"
