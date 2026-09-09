"""Tests for `_print_summary` three-state exit codes in batch."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from mpt_autopilot.batch.run import _print_summary
from mpt_autopilot.batch.settings import Settings


def _fake_settings() -> Settings:
    s = MagicMock(spec=Settings)
    s.log_file = Path("/tmp/fake.log")
    s.telegram_token = ""
    s.telegram_chat_id = ""
    s.telegram_prefix = ""
    return s


def _run(ok_items, failed_items, skipped_items):
    with patch("mpt_autopilot.batch.run.notify.alert"):
        with patch("mpt_autopilot.batch.run.log"):
            return _print_summary(
                ok=list(ok_items),
                failed=list(failed_items),
                skipped=list(skipped_items),
                settings=_fake_settings(),
                started_at=0.0,
            )


def test_all_ok_returns_zero():
    assert _run(["a", "b"], [], []) == 0


def test_all_failed_returns_one():
    assert _run([], ["a", "b"], []) == 1


def test_partial_returns_two():
    assert _run(["a", "b"], ["c"], []) == 2


def test_only_skipped_returns_zero():
    assert _run([], [], ["a", "b"]) == 0
