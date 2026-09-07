"""Tests for batch/api.py — httpx exception handling."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest


def _settings(**overrides):
    """Build a minimal Settings stub."""
    s = MagicMock(
        api_url="http://localhost:8080",
        max_wait_seconds=30,
        stuck_threshold_seconds=60,
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


# ── health_check ──────────────────────────────────────────────────────────────


def test_health_check_returns_false_on_http_error():
    from mpt_autopilot.batch.api import health_check

    settings = _settings()
    with patch("mpt_autopilot.batch.api.httpx.get") as mock_get:
        mock_get.side_effect = httpx.ConnectError("connection refused")
        assert health_check(settings) is False


def test_health_check_returns_false_on_http_status_error():
    from mpt_autopilot.batch.api import health_check

    settings = _settings()
    with patch("mpt_autopilot.batch.api.httpx.get") as mock_get:
        mock_get.side_effect = httpx.HTTPStatusError(
            "500",
            request=httpx.Request("GET", "http://x"),
            response=httpx.Response(500),
        )
        assert health_check(settings) is False


def test_health_check_returns_false_on_timeout():
    from mpt_autopilot.batch.api import health_check

    settings = _settings()
    with patch("mpt_autopilot.batch.api.httpx.get") as mock_get:
        mock_get.side_effect = httpx.TimeoutException("timed out")
        assert health_check(settings) is False


def test_health_check_returns_true_on_success():
    from mpt_autopilot.batch.api import health_check

    settings = _settings()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"data": []}
    with patch("mpt_autopilot.batch.api.httpx.get") as mock_get:
        mock_get.return_value = mock_response
        assert health_check(settings) is True


# ── submit_job ────────────────────────────────────────────────────────────────


def test_submit_job_raises_runtime_error_on_http_status_error():
    from mpt_autopilot.batch.api import submit_job

    settings = _settings()
    with patch("mpt_autopilot.batch.api.httpx.post") as mock_post:
        mock_post.side_effect = httpx.HTTPStatusError(
            "400",
            request=httpx.Request("POST", "http://x"),
            response=httpx.Response(400, text="bad request"),
        )
        with pytest.raises(RuntimeError, match="400"):
            submit_job({"test": "data"}, settings)


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
    with patch("mpt_autopilot.batch.api.httpx.post") as mock_post:
        mock_post.return_value = mock_response
        with pytest.raises(RuntimeError, match="no task_id"):
            submit_job({"test": "data"}, settings)


def test_submit_job_returns_task_id_on_success():
    from mpt_autopilot.batch.api import submit_job

    settings = _settings()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"data": {"task_id": "task-abc123"}}
    with patch("mpt_autopilot.batch.api.httpx.post") as mock_post:
        mock_post.return_value = mock_response
        assert submit_job({"test": "data"}, settings) == "task-abc123"


# ── wait_for_task ─────────────────────────────────────────────────────────────


def test_wait_for_task_raises_runtime_error_on_http_error():
    from mpt_autopilot.batch.api import wait_for_task

    settings = _settings()
    with patch("mpt_autopilot.batch.api.httpx.get") as mock_get:
        mock_get.side_effect = httpx.ConnectError("connection refused")
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
    with patch("mpt_autopilot.batch.api.httpx.get") as mock_get:
        mock_get.return_value = mock_response
        with pytest.raises(KeyError, match="data"):
            wait_for_task("test-task", settings)
