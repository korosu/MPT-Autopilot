"""Tests for batch/api.py — httpx exception handling.

api.py talks to the MPT API through `_http_utils.get_shared_client()`, so the
tests patch that instead of `httpx.get`/`httpx.post` at module level — a
module-level patch would pass while the production call path (the shared
client, with connection reuse) was broken.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from mpt_autopilot.batch.api import PayloadRejected, submit_job


def _settings(**overrides):
    """Build a minimal Settings stub."""
    s = MagicMock(
        api_url="http://localhost:8080",
        max_wait_seconds=30,
        stuck_threshold_seconds=60,
        poll_interval_seconds=0,
        api_key="",
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _shared_client():
    """A MagicMock httpx.Client; patch get_shared_client() with its return."""
    with patch("mpt_autopilot.batch.api.get_shared_client") as gsc:
        client = MagicMock()
        gsc.return_value = client
        yield client


# ── health_check ──────────────────────────────────────────────────────────────


def test_health_check_returns_false_on_http_error():
    from mpt_autopilot.batch.api import health_check

    settings = _settings()
    for client in _shared_client():
        client.get.side_effect = httpx.ConnectError("connection refused")
        assert health_check(settings) is False


def test_health_check_returns_false_on_http_status_error():
    from mpt_autopilot.batch.api import health_check

    settings = _settings()
    for client in _shared_client():
        client.get.side_effect = httpx.HTTPStatusError(
            "500",
            request=httpx.Request("GET", "http://x"),
            response=httpx.Response(500),
        )
        assert health_check(settings) is False


def test_health_check_returns_false_on_timeout():
    from mpt_autopilot.batch.api import health_check

    settings = _settings()
    for client in _shared_client():
        client.get.side_effect = httpx.TimeoutException("timed out")
        assert health_check(settings) is False


def test_health_check_returns_true_on_success():
    from mpt_autopilot.batch.api import health_check

    settings = _settings()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"data": []}
    for client in _shared_client():
        client.get.return_value = mock_response
        assert health_check(settings) is True


def test_health_check_returns_false_on_top_level_array():
    """A JSON array at the top level used to AttributeError outside the except.

    body.get("data") on a list raises AttributeError, which the function's
    `except (httpx.HTTPError, ValueError)` does not catch — the audit's M-32.
    """
    from mpt_autopilot.batch.api import health_check

    settings = _settings()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = ["gateway", "error", "page"]
    for client in _shared_client():
        client.get.return_value = mock_response
        assert health_check(settings) is False


# ── submit_job ────────────────────────────────────────────────────────────────


def _status_error(status: int, body: dict) -> httpx.HTTPStatusError:
    response = httpx.Response(status, json=body, request=httpx.Request("POST", "http://x"))
    return httpx.HTTPStatusError(str(status), request=response.request, response=response)


def test_submit_job_400_raises_payload_rejected_with_field_detail():
    """R6: a 4xx names the offending field from MPT's own error body and must
    NOT be retried (B-1 burned 3×180s on exactly this class of error)."""
    settings = _settings()
    err = _status_error(
        400,
        {
            "status": 400,
            "data": [
                {
                    "loc": ["body", "video_transition_mode"],
                    "msg": "Input should be 'Shuffle' or ...",
                }
            ],
            "message": "field required",
        },
    )
    for client in _shared_client():
        client.post.side_effect = err
        with pytest.raises(PayloadRejected, match="video_transition_mode"):
            submit_job({"test": "data"}, settings)


def test_submit_job_500_stays_retryable():
    """R6: only 4xx is fatal; a 5xx is transient trouble the retry loop exists for."""
    settings = _settings()
    for client in _shared_client():
        client.post.side_effect = _status_error(500, {"status": 500, "message": "boom"})
        with pytest.raises(RuntimeError) as exc_info:
            submit_job({"test": "data"}, settings)
        assert not isinstance(exc_info.value, PayloadRejected)


def test_submit_job_429_stays_retryable():
    """R6: 429 (MPT's queue is full) is rate limiting, not a contract error."""
    settings = _settings()
    for client in _shared_client():
        client.post.side_effect = _status_error(429, {"status": 429, "message": "queue full"})
        with pytest.raises(RuntimeError) as exc_info:
            submit_job({"test": "data"}, settings)
        assert not isinstance(exc_info.value, PayloadRejected)


def test_api_key_is_sent_as_x_api_key_header():
    """R6: MPT can require x-api-key on every route; without the header a
    key-enabled MPT is unusable from Autopilot."""
    settings = _settings(api_key="s3cret")
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"data": {"task_id": "task-1"}}
    for client in _shared_client():
        client.post.return_value = mock_response
        assert submit_job({"test": "data"}, settings) == "task-1"
        assert client.post.call_args.kwargs["headers"]["x-api-key"] == "s3cret"


def test_no_auth_header_without_an_api_key():
    settings = _settings(api_key="")
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"data": {"task_id": "task-1"}}
    for client in _shared_client():
        client.post.return_value = mock_response
        submit_job({"test": "data"}, settings)
        assert client.post.call_args.kwargs["headers"] == {}


def test_submit_job_raises_key_error_on_bad_json():
    """A KeyError from parsing the response is wrapped as RuntimeError with context.

    The original `except Exception` bug swallowed KeyError silently. Now it is
    caught explicitly and re-raised as RuntimeError (with the original KeyError
    chained via `from exc`), so the caller gets a clear message instead of a
    bare key name.
    """
    from mpt_autopilot.batch.api import submit_job

    settings = _settings()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"no_data_key": True}
    for client in _shared_client():
        client.post.return_value = mock_response
        with pytest.raises(RuntimeError, match="no task_id"):
            submit_job({"test": "data"}, settings)


def test_submit_job_returns_task_id_on_success():
    from mpt_autopilot.batch.api import submit_job

    settings = _settings()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"data": {"task_id": "task-abc123"}}
    for client in _shared_client():
        client.post.return_value = mock_response
        assert submit_job({"test": "data"}, settings) == "task-abc123"


# ── wait_for_task ─────────────────────────────────────────────────────────────


def test_wait_for_task_raises_runtime_error_on_http_error():
    from mpt_autopilot.batch.api import wait_for_task

    settings = _settings()
    for client in _shared_client():
        client.get.side_effect = httpx.ConnectError("connection refused")
        with pytest.raises(RuntimeError, match="API error while polling"):
            wait_for_task("test-task", settings)


def test_wait_for_task_raises_key_error_on_bad_json():
    """A KeyError from parsing should propagate, not be masked as RuntimeError.

    Before the fix: `except Exception` caught this and re-raised as
    RuntimeError, hiding the real parse error. After the fix: `httpx.HTTPError`
    doesn't match KeyError, so it propagates naturally.
    """
    from mpt_autopilot.batch.api import wait_for_task

    settings = _settings()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"no_data_key": True}
    for client in _shared_client():
        client.get.return_value = mock_response
        with pytest.raises(KeyError, match="data"):
            wait_for_task("test-task", settings)


def test_wait_for_task_without_log_is_silent(capsys):
    """The default `log` is None (audit L: `log=print` as a default), so a
    caller that passes nothing must not write to stdout."""
    from mpt_autopilot.batch.api import wait_for_task

    settings = _settings()
    for client in _shared_client():
        client.get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"data": {"progress": 100, "state": 1, "task_id": "t"}},
        )
        result = wait_for_task("t", settings)
    assert result["progress"] == 100
    assert capsys.readouterr().out == ""
