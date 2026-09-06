"""Tests for api.py state handling — verifies state=4 (PROCESSING) is accepted."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# Test the state transition logic: PROCESSING (4) should continue, FAILED (-1) should raise


def test_wait_for_task_accepts_state_4_processing():
    """State 4 (PROCESSING) should continue polling, not raise RuntimeError.

    This is the core bug fix: mpt-batch was treating state=4 as failure
    because it checked `state not in (0, 1)`. MPT sets state=4 immediately
    on task creation.
    """
    from mpt_autopilot.batch.api import wait_for_task

    # We need minimal settings for the test
    settings = MagicMock(
        api_url="http://localhost:8080",
        max_wait_seconds=30,
        stuck_threshold_seconds=60,
    )

    log_calls = []

    def mock_log(msg):
        log_calls.append(msg)

    # Simulate: task starts with state=4 (PROCESSING), then completes
    mock_responses = [
        # First poll: state=4 is PROCESSING (alive), progress low
        MagicMock(json=lambda: {"data": {"progress": 5, "state": 4}}),
        # Second poll: still processing
        MagicMock(json=lambda: {"data": {"progress": 50, "state": 4}}),
        # Third poll: complete
        MagicMock(json=lambda: {"data": {"progress": 100, "state": 1, "task_id": "test-123"}}),
    ]

    with patch("mpt_autopilot.batch.api.httpx.get") as mock_get:
        mock_get.side_effect = mock_responses
        result = wait_for_task("test-task", settings, log=mock_log)

    assert result["progress"] == 100
    assert "state=4" in log_calls[0] or "progress=5%" in log_calls[0]


def test_wait_for_task_raises_on_state_minus_1():
    """State -1 (FAILED) should raise RuntimeError."""
    from mpt_autopilot.batch.api import wait_for_task

    settings = MagicMock(
        api_url="http://localhost:8080",
        max_wait_seconds=30,
        stuck_threshold_seconds=60,
    )

    mock_response = MagicMock(json=lambda: {"data": {"progress": 30, "state": -1}})

    with patch("mpt_autopilot.batch.api.httpx.get") as mock_get:
        mock_get.return_value = mock_response
        with pytest.raises(RuntimeError, match="task failed.*state=-1"):
            wait_for_task("test-task", settings)


def _settings() -> MagicMock:
    return MagicMock(
        api_url="http://localhost:8080",
        max_wait_seconds=30,
        stuck_threshold_seconds=60,
    )


def _respond(body: dict) -> MagicMock:
    """A single polling response whose .json() yields {"data": body}."""
    return MagicMock(json=lambda: {"data": body})


def test_wait_for_task_copes_with_string_progress():
    """
    A string progress used to leak a raw TypeError out of the poll loop:
    `'>' not supported between instances of 'str' and 'int'` at
    `progress > max_progress_seen`. That TypeError is not in the retry
    except clauses, so it killed the whole batch run instead of failing the task.
    """
    from mpt_autopilot.batch.api import wait_for_task

    with patch("mpt_autopilot.batch.api.httpx.get") as mock_get:
        mock_get.side_effect = [
            _respond({"progress": "50", "state": 4}),
            _respond({"progress": "100", "state": 1, "task_id": "t"}),
        ]
        result = wait_for_task("t", _settings())

    assert result["progress"] == "100"


def test_wait_for_task_rejects_non_numeric_progress():
    """A non-numeric progress must name the field, not raise a bare TypeError."""
    from mpt_autopilot.batch.api import wait_for_task

    with patch("mpt_autopilot.batch.api.httpx.get") as mock_get:
        mock_get.return_value = _respond({"progress": "indeterminate", "state": 4})
        with pytest.raises(ValueError, match="progress"):
            wait_for_task("t", _settings())


def test_wait_for_task_fails_fast_on_string_state():
    """
    `state == -1` is False for the string "failed", so the loop polled until
    max_wait_seconds (2400s by default) instead of failing fast.
    """
    from mpt_autopilot.batch.api import wait_for_task

    settings = _settings()
    settings.max_wait_seconds = 2

    with patch("mpt_autopilot.batch.api.httpx.get") as mock_get:
        mock_get.return_value = _respond({"progress": 30, "state": "failed"})
        with pytest.raises(ValueError, match="state"):
            wait_for_task("t", settings)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0), (50, 50), (-1, -1), ("50", 50), ("-1", -1), (50.0, 50)],
)
def test_as_int_accepts_numbers_and_numeric_strings(value, expected):
    from mpt_autopilot.batch.api import _as_int

    assert _as_int(value, "progress") == expected


@pytest.mark.parametrize("value", [True, "failed", None, [1], {"a": 1}])
def test_as_int_rejects_non_numeric_values(value):
    """Boolean is rejected explicitly: int(True) == 1 would silently mask it."""
    from mpt_autopilot.batch.api import _as_int

    with pytest.raises(ValueError):
        _as_int(value, "state")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
