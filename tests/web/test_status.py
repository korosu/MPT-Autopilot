"""
Tests for the MoneyPrinterTurbo health probe.

httpx.MockTransport stands in for the network, so nothing here leaves the
process — including the ConnectError case, which the transport raises instead
of the OS.

The endpoint under test is the one `batch` actually uses
(`GET /api/v1/tasks`), not only `/ping`: a deployment whose MPT predates the
/ping route (the user's VPS runs 1.3.3 — /ping 404s while /api/v1/tasks
answers 200 with a task list) must be reported up, not amber.
"""

from __future__ import annotations

import httpx

from mpt_autopilot.web.status import MptStatus, probe

TASK_LIST = '{"status": 200, "message": "success", "data": {"tasks": [], "total": 0}}'


def test_a_missing_batch_section_reports_not_configured():
    status = probe(None)
    assert status == MptStatus(False, False, False, None, None, None, None)
    assert status.as_dict() == {
        "configured": False,
        "up": False,
        "reachable": False,
        "status_code": None,
        "latency_ms": None,
        "api_url": None,
        "host": None,
        "via": None,
    }


def test_a_task_list_answer_means_up():
    """The user's deployment: /api/v1/tasks answers, /ping does not exist."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/api/v1/tasks"):
            return httpx.Response(200, text=TASK_LIST)
        return httpx.Response(404, text='{"detail":"Not Found"}')

    status = probe("http://127.0.0.1:8080", transport=httpx.MockTransport(handler))
    assert (status.up, status.via, status.status_code) == (True, "tasks", 200)
    assert status.reachable is True
    assert status.host == "127.0.0.1:8080"
    assert status.latency_ms is not None and status.latency_ms >= 0


def test_a_data_list_shape_is_also_a_task_list():
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/api/v1/tasks"):
            return httpx.Response(200, text='{"data": [1, 2, 3]}')
        return httpx.Response(500)

    status = probe("http://mpt.local:8080", transport=httpx.MockTransport(handler))
    assert (status.up, status.via) == (True, "tasks")


def test_a_pong_answer_means_up_via_ping():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, text="pong")

    status = probe("http://127.0.0.1:8080", transport=httpx.MockTransport(handler))
    assert (status.up, status.status_code, status.host, status.via) == (
        True,
        200,
        "127.0.0.1:8080",
        "ping",
    )
    assert status.reachable is True
    assert status.latency_ms is not None and status.latency_ms >= 0


def test_a_trailing_slash_in_api_url_still_hits_the_endpoints():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "http://mpt.local:8080/api/v1/tasks":
            return httpx.Response(200, text=TASK_LIST)
        raise AssertionError(f"unexpected second call: {url}")

    status = probe("http://mpt.local:8080/", transport=httpx.MockTransport(handler))
    assert status.up is True


def test_a_wrong_shaped_task_body_falls_back_to_ping():
    """A 200 with an error-shaped body is not MPT — /ping gets the deciding vote."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/api/v1/tasks"):
            return httpx.Response(200, text='{"detail": "gateway error"}')
        return httpx.Response(200, text="pong")

    status = probe("http://127.0.0.1:8080", transport=httpx.MockTransport(handler))
    assert (status.up, status.via, status.status_code) == (True, "ping", 200)


def test_a_key_protected_api_is_reachable_but_not_up():
    """401 on /api/v1/tasks and no /ping: not a healthy MPT for this config."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/api/v1/tasks"):
            return httpx.Response(401)
        return httpx.Response(404)

    status = probe("http://127.0.0.1:8080", transport=httpx.MockTransport(handler))
    assert (status.up, status.reachable, status.status_code, status.via) == (False, True, 401, None)


def test_an_answer_that_is_not_mpt_is_reachable_but_not_up():
    """404 on both endpoints proves the port is taken — but not by MPT 1.3.3+."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    status = probe("http://127.0.0.1:8080", transport=httpx.MockTransport(handler))
    assert (status.up, status.reachable, status.status_code, status.via) == (False, True, 404, None)


def test_a_connection_refusal_means_down():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    status = probe("http://127.0.0.1:9999", transport=httpx.MockTransport(handler))
    assert (status.up, status.status_code, status.latency_ms, status.reachable, status.via) == (
        False,
        None,
        None,
        False,
        None,
    )
    assert status.host == "127.0.0.1:9999"


def test_a_slow_task_endpoint_still_falls_back_to_ping():
    """A timeout on the tasks listing must not be reported as "down" by itself."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/api/v1/tasks"):
            raise httpx.ReadTimeout("too slow")
        return httpx.Response(200, text="pong")

    status = probe("http://127.0.0.1:8080", transport=httpx.MockTransport(handler))
    assert (status.up, status.via) == (True, "ping")
