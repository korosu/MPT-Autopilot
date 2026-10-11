"""
Regression tests for the 2026-10-02 shared-infrastructure findings (M-2…M-7).

These cover the pieces every stage depends on and that had no tests at all:
lock stealing, Logger reconfiguration, the retry helper's exhaustion contract,
and the config helpers' error types.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from mpt_autopilot import config as shared_config
from mpt_autopilot._http_utils import close_shared_client, get_shared_client, retrying_request
from mpt_autopilot.config import ConfigError
from mpt_autopilot.lock import _steal_stale, file_lock
from mpt_autopilot.logger import Logger, configure_root

# ── M-3: stale-lock stealing is atomic ───────────────────────────────────────


def test_only_one_waiter_can_steal_a_stale_lock(tmp_path):
    """
    An unlink()-based steal lets two processes both proceed. Renaming means the
    loser sees the lock gone and goes back through O_EXCL, so only one of them
    ever observes a successful steal.
    """
    lock_path = tmp_path / "seen.txt.lock"
    lock_path.write_text("", encoding="utf-8")

    _steal_stale(lock_path)
    assert not lock_path.exists()

    # Second steal of an already-stolen lock must be a silent no-op, not a crash
    # and not a "success" that would let a second holder in.
    _steal_stale(lock_path)
    assert not lock_path.exists()


def test_steal_leaves_no_debris(tmp_path):
    lock_path = tmp_path / "seen.txt.lock"
    lock_path.write_text("", encoding="utf-8")

    _steal_stale(lock_path)

    assert list(tmp_path.iterdir()) == []


def test_an_expired_lock_is_taken_over(tmp_path):
    target = tmp_path / "seen.txt"
    lock_path = tmp_path / "seen.txt.lock"
    lock_path.write_text("", encoding="utf-8")
    old = time.time() - 3600
    os.utime(lock_path, (old, old))

    with file_lock(target, timeout=1, stale_after=60):
        assert lock_path.exists()  # ours now

    assert not lock_path.exists()


def test_stale_after_is_honoured_per_call(tmp_path):
    """
    batch's run lock tolerates half an hour; seen.txt's tolerates a minute. With
    a high stale_after the same lock file must still be treated as live.
    """
    target = tmp_path / "jobs.yaml"
    lock_path = tmp_path / "jobs.yaml.lock"
    lock_path.write_text("", encoding="utf-8")
    old = time.time() - 120
    os.utime(lock_path, (old, old))

    with file_lock(target, timeout=0, stale_after=1800):
        pass  # gave up waiting and proceeded unlocked

    assert lock_path.exists()  # not stolen — still considered live
    lock_path.unlink()


# ── M-2: Logger honours a changed log file ───────────────────────────────────


def _close(name: str) -> None:
    logger = logging.getLogger(name)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def test_reusing_a_logger_name_with_a_new_file_switches_files(tmp_path):
    name = "mpt_autopilot.test.switch"
    try:
        Logger(tmp_path / "first.log", 1024, name=name).info("one")
        Logger(tmp_path / "second.log", 1024, name=name).info("two")

        assert "two" in (tmp_path / "second.log").read_text(encoding="utf-8")
        assert "two" not in (tmp_path / "first.log").read_text(encoding="utf-8")
    finally:
        _close(name)


def test_reusing_a_logger_name_with_the_same_file_keeps_one_handler(tmp_path):
    name = "mpt_autopilot.test.same"
    try:
        Logger(tmp_path / "x.log", 1024, name=name)
        Logger(tmp_path / "x.log", 1024, name=name).info("once")

        assert (tmp_path / "x.log").read_text(encoding="utf-8").count("once") == 1
    finally:
        _close(name)


# ── M-1 / part 9: diagnostics reach stderr at all ────────────────────────────


def test_configure_root_is_idempotent():
    root = logging.getLogger()
    had = list(root.handlers)
    try:
        root.handlers = []
        configure_root()
        configure_root()
        assert len(root.handlers) == 1
    finally:
        root.handlers = had


def test_configure_root_sends_module_info_records_to_stderr(capsys):
    """The point of M-1: a `logging.getLogger(__name__).info(...)` in any of
    the thirteen stage modules used to be dropped outright; with the root
    handler installed it must reach stderr, formatted."""
    root = logging.getLogger()
    had = list(root.handlers)
    try:
        for h in had:
            root.removeHandler(h)
        configure_root()
        logging.getLogger("mpt_autopilot.pilot.refill").info("skip duplicate: topic x")
        err = capsys.readouterr().err
        assert "skip duplicate: topic x" in err
        assert "[INFO]" in err
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        root.handlers = had


# ── M-5 / M-6: the retry helper's exhaustion and Retry-After contract ────────


class _Resp:
    def __init__(self, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = ""
        self.reason_phrase = "Too Many Requests"
        self.request = MagicMock()
        self.is_success = 200 <= status_code < 400

    def raise_for_status(self):
        if not self.is_success:
            raise RuntimeError(self.status_code)


class _Client:
    def __init__(self, responses):
        self._responses = responses
        self.calls = 0

    def request(self, method, url, **kwargs):
        self.calls += 1
        return self._responses[min(self.calls - 1, len(self._responses) - 1)]


def test_exhausted_429_raises_the_same_type_as_other_http_failures(monkeypatch):
    """
    It used to raise RuntimeError while an exhausted transient status raised
    httpx.HTTPStatusError, so a caller watching for a rate-limit condition had
    to catch two unrelated types.
    """
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    client = _Client([_Resp(429)])

    with pytest.raises(httpx.HTTPStatusError, match="429"):
        retrying_request(client, "POST", "http://e.test", max_retries=1)


def test_retry_after_zero_does_not_busy_loop(monkeypatch):
    delays: list[float] = []
    monkeypatch.setattr(time, "sleep", delays.append)
    client = _Client([_Resp(429, {"Retry-After": "0"}), _Resp(200)])

    retrying_request(
        client,
        "POST",
        "http://e.test",
        max_retries=2,
        backoff_base=2.0,
        respect_retry_after=True,
    )

    assert delays == [2.0]  # floored at backoff_base, not 0


def test_retry_after_still_wins_when_longer(monkeypatch):
    delays: list[float] = []
    monkeypatch.setattr(time, "sleep", delays.append)
    client = _Client([_Resp(429, {"Retry-After": "7"}), _Resp(200)])

    retrying_request(
        client,
        "POST",
        "http://e.test",
        max_retries=2,
        backoff_base=2.0,
        respect_retry_after=True,
    )

    assert delays == [7.0]


# ── M-4: the shared client's timeout is not frozen at creation ───────────────


def test_shared_client_timeout_follows_the_environment(monkeypatch):
    close_shared_client()
    try:
        monkeypatch.setenv("LLM_TIMEOUT", "5")
        first = get_shared_client()
        assert first.timeout.read == 5.0

        # A stage that loads its .env after the first notify.alert() must not be
        # stuck with the timeout that call happened to create.
        monkeypatch.setenv("LLM_TIMEOUT", "180")
        second = get_shared_client()
        assert second is first
        assert second.timeout.read == 180.0
    finally:
        close_shared_client()


# ── Shared L-block: error types and loopback detection ───────────────────────


def test_suffix_dir_names_the_configured_languages(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text('langs:\n  en:\n    file_suffix: ""\n', encoding="utf-8")
    cfg = shared_config.load(config, reload=True)

    with pytest.raises(ConfigError, match="unknown language 'de'"):
        cfg.suffix_dir(Path("exports"), "de")


def test_suffix_dir_appends_the_language_suffix(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text('langs:\n  es:\n    file_suffix: "_es"\n', encoding="utf-8")
    cfg = shared_config.load(config, reload=True)

    assert cfg.suffix_dir(Path("/srv/exports"), "es") == Path("/srv/exports_es")


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080", "http://localhost/v1", "http://[::1]/v1"])
def test_loopback_http_is_not_warned_about(url, caplog):
    shared_config.clear_cache()
    with caplog.at_level(logging.WARNING):
        shared_config.warn_cleartext(url, "LLM_BASE_URL")
    assert caplog.text == ""


def test_http_on_all_interfaces_is_warned_about(caplog):
    """
    0.0.0.0 means every interface, so the endpoint is reachable off-host and the
    traffic to it is in the clear. It used to be whitelisted as loopback.
    """
    shared_config.clear_cache()
    with caplog.at_level(logging.WARNING):
        shared_config.warn_cleartext("http://0.0.0.0:8080", "batch.api_url")
    assert "plain HTTP" in caplog.text


def test_cleartext_warning_is_emitted_once_per_url(caplog):
    shared_config.clear_cache()
    with caplog.at_level(logging.WARNING):
        shared_config.warn_cleartext("http://0.0.0.0:8080", "batch.api_url")
        shared_config.warn_cleartext("http://0.0.0.0:8080", "batch.api_url")
    assert caplog.text.count("plain HTTP") == 1
