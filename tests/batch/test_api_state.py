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
    from mpt_batch.engine.api import wait_for_task

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

    with patch("mpt_batch.engine.api.requests.get") as mock_get:
        mock_get.side_effect = mock_responses
        result = wait_for_task("test-task", settings, log=mock_log)

    assert result["progress"] == 100
    assert "state=4" in log_calls[0] or "progress=5%" in log_calls[0]


def test_wait_for_task_raises_on_state_minus_1():
    """State -1 (FAILED) should raise RuntimeError."""
    from mpt_batch.engine.api import wait_for_task

    settings = MagicMock(
        api_url="http://localhost:8080",
        max_wait_seconds=30,
        stuck_threshold_seconds=60,
    )

    mock_response = MagicMock(json=lambda: {"data": {"progress": 30, "state": -1}})

    with patch("mpt_batch.engine.api.requests.get") as mock_get:
        mock_get.return_value = mock_response
        with pytest.raises(RuntimeError, match="task failed.*state=-1"):
            wait_for_task("test-task", settings)
