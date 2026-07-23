from __future__ import annotations

from pathlib import Path

import requests

from mpt_batch.engine.notify import alert
from mpt_batch.engine.settings import Settings


def _settings(
    *,
    token: str = "123:ABC",
    chat_id: str = "-100123",
    prefix: str = "test",
) -> Settings:
    return Settings(
        api_url="http://localhost:8080",
        mpt_storage=Path("/tmp/storage"),
        output_dir=Path("/tmp/output"),
        seen_file=Path("/tmp/seen.txt"),
        langs={},
        jobs_dir=None,
        jobs=None,
        voice_pool={},
        log_file=Path("/tmp/log.txt"),
        log_max_bytes=5_000_000,
        max_wait_seconds=2400,
        stuck_threshold_seconds=3600,
        max_retries=3,
        retry_delay_seconds=180,
        max_consecutive_failures=3,
        cache_cleanup_enabled=True,
        cache_cleanup_interval=6,
        telegram_token=token,
        telegram_chat_id=chat_id,
        telegram_prefix=prefix,
    )


def test_sends_to_telegram_api(monkeypatch):
    calls = []

    def fake_post(url, **kw):
        calls.append({"url": url, "json": kw.get("json")})
        return type("R", (), {"ok": True})()

    monkeypatch.setattr("mpt_batch.engine.notify.requests.post", fake_post)
    alert("hello", _settings())
    assert len(calls) == 1
    assert "api.telegram.org/bot123:ABC/sendMessage" in calls[0]["url"]
    assert "[test] hello" in calls[0]["json"]["text"]


def test_missing_token_skips(monkeypatch):
    calls = []

    def fake_post(url, **kw):
        calls.append(1)

    monkeypatch.setattr("mpt_batch.engine.notify.requests.post", fake_post)
    alert("hi", _settings(token=""))
    assert calls == []


def test_missing_chat_id_skips(monkeypatch):
    calls = []

    def fake_post(url, **kw):
        calls.append(1)

    monkeypatch.setattr("mpt_batch.engine.notify.requests.post", fake_post)
    alert("hi", _settings(chat_id=""))
    assert calls == []


def test_exception_is_swallowed(monkeypatch):
    def boom(*a, **kw):
        raise requests.ConnectionError("down")

    monkeypatch.setattr("mpt_batch.engine.notify.requests.post", boom)
    alert("hi", _settings())  # Does not raise


# ponytail: reused Settings structure already has all required fields
