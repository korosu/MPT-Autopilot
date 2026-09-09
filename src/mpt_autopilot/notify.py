"""
notify.py — the single Telegram notifier shared by every MPT Autopilot stage.

Replaces four byte-identical copies that used to live in each of the merged
tools. Accepts any settings object that carries the three Telegram fields, so
every stage's own Settings dataclass works without a conversion step.

Silently does nothing when TELEGRAM_TOKEN / TELEGRAM_CHAT_ID are unset in .env.
Never raises — a failed alert must not break the run that triggered it.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable
from urllib.parse import quote as _url_quote

from mpt_autopilot._http_utils import get_shared_client

_logger = logging.getLogger(__name__)


@runtime_checkable
class TelegramSettings(Protocol):
    """The only part of a stage's Settings this module needs."""

    @property
    def telegram_token(self) -> str: ...

    @property
    def telegram_chat_id(self) -> str: ...

    @property
    def telegram_prefix(self) -> str: ...


def _redact(text: object, token: str) -> str:
    """
    Remove the bot token from any loggable string.

    Covers both plain and URL-encoded variants (e.g. a token with ':' becomes
    '%3A' inside a URL path). A passive proxy does not see the path — it is
    inside the TLS tunnel after CONNECT — but redaction is cheap insurance
    against an MITM proxy or a log aggregator that records URLs.
    """
    out = str(text)
    if token:
        out = out.replace(token, "[token]")
        encoded_token = _url_quote(token, safe="")
        if encoded_token != token:
            out = out.replace(encoded_token, "[token]")
    return out


def alert(msg: str, settings: TelegramSettings) -> None:
    if not settings.telegram_token or not settings.telegram_chat_id:
        return
    text = f"[{settings.telegram_prefix}] {msg}" if settings.telegram_prefix else msg
    url = f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage"
    try:
        r = get_shared_client().post(
            url,
            json={"chat_id": settings.telegram_chat_id, "text": text},
            timeout=10,
        )
        if not r.is_success:
            _logger.warning(
                "[%s] Telegram returned %s: %s",
                settings.telegram_prefix,
                r.status_code,
                _redact(r.text.strip()[:200], settings.telegram_token),
            )
    except Exception as exc:
        _logger.warning(
            "[%s] Telegram send failed: %s",
            settings.telegram_prefix,
            _redact(exc, settings.telegram_token),
        )
