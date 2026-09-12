from __future__ import annotations

from pathlib import Path

import httpx

from mpt_autopilot.batch.settings import Settings
from mpt_autopilot.notify import alert


def _settings(
    *,
    token: str = "123:ABC",
    chat_id: str = "-100123",
    prefix: str = "test",
) -> Settings:
    return Settings(
        api_url="http://localhost:8080",
        mpt_storage=Path("/tmp/storage"),
        mpt_songs_dir=Path("/tmp/resource/songs"),
        output_dir=Path("/tmp/output"),
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
        seen_max_mb=64,
        telegram_token=token,
        telegram_chat_id=chat_id,
        telegram_prefix=prefix,
    )


def _fake_response(success: bool = True):
    return type("R", (), {"is_success": success})()


def _mock_shared_client(monkeypatch, post_fn):
    """Replace get_shared_client with a mock that has a .post method."""

    class _FakeClient:
        post = post_fn

    monkeypatch.setattr("mpt_autopilot.notify.get_shared_client", lambda: _FakeClient())


def test_sends_to_telegram_api(monkeypatch):
    calls = []

    def fake_post(self, url, **kw):
        calls.append({"url": url, "json": kw.get("json")})
        return _fake_response()

    _mock_shared_client(monkeypatch, fake_post)
    alert("hello", _settings())
    assert len(calls) == 1
    assert "api.telegram.org/bot123:ABC/sendMessage" in calls[0]["url"]
    assert "[test] hello" in calls[0]["json"]["text"]


def test_missing_token_skips(monkeypatch):
    calls = []

    def fake_post(self, url, **kw):
        calls.append(1)

    _mock_shared_client(monkeypatch, fake_post)
    alert("hi", _settings(token=""))
    assert calls == []


def test_missing_chat_id_skips(monkeypatch):
    calls = []

    def fake_post(self, url, **kw):
        calls.append(1)

    _mock_shared_client(monkeypatch, fake_post)
    alert("hi", _settings(chat_id=""))
    assert calls == []


def test_exception_is_swallowed(monkeypatch):
    def boom(self, url, **kw):
        raise httpx.ConnectError("down")

    _mock_shared_client(monkeypatch, boom)
    alert("hi", _settings())  # Does not raise


# --- ponytail: URL-encoded token redaction (Lane B 4.3) ---


def test_redact_plain_token():
    from mpt_autopilot.notify import _redact

    assert _redact("key=123:ABC", "123:ABC") == "key=[token]"


def test_redact_url_encoded_token():
    from mpt_autopilot.notify import _redact

    text = "URL /bot123%3AABC/send was logged"
    assert _redact(text, "123:ABC") == "URL /bot[token]/send was logged"


def test_redact_no_token_leaves_text_unchanged():
    from mpt_autopilot.notify import _redact

    assert _redact("nothing to see here", "") == "nothing to see here"


def test_redact_empty_token():
    from mpt_autopilot.notify import _redact

    assert _redact("same text", "") == "same text"


# ponytail: reused Settings structure already has all required fields
