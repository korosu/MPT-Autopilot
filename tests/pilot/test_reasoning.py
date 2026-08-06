#!/usr/bin/env python3
"""
test_reasoning.py — tests for reasoning-model support in generator/llm.py.

Covers the bug this guards against: a reasoning model (thinking on by
default) burns the whole max_tokens budget on `reasoning_content` and
returns `content: null`, which used to crash with a raw AttributeError/
KeyError deep inside call_llm(). It should now raise a clear ValueError,
and the reasoning_enabled config flag should be a true tri-state
(None / True / False), not a two-state bool.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

src_dir = Path(__file__).parent.parent / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from shorts_pilot.generator import llm  # noqa: E402
from shorts_pilot.generator.settings import Settings  # noqa: E402


def make_settings(**overrides: Any) -> Settings:
    # Explicit dict[str, Any] annotation matters here: without it, pyright
    # infers the dict() call's type from the literal kwargs below (a narrow
    # union of str/int/list/dict/None), and Settings(**base) then fails to
    # typecheck against the dataclass's actual per-field types.
    base: dict[str, Any] = dict(
        api_key="k",
        base_url="https://selfhosted.example/v1",
        model="r1",
        telegram_token="",
        telegram_chat_id="",
        telegram_prefix="p",
        generate_count=21,
        refill_threshold=10,
        scan_dirs=[],
        langs={},
        jobs_dir=None,
        seen_dir=None,
    )
    base.update(overrides)
    return Settings(**base)


def _fake_resp(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = lambda: None
    resp.json.return_value = payload
    return resp


def test_default_reasoning_enabled_is_none():
    """Omitting the key from config.yaml must stay None, not coerce to False."""
    s = make_settings()
    assert s.reasoning_enabled is None


def test_none_content_raises_clean_valueerror_not_attributeerror():
    """The exact repro: finish_reason=length, content=null, reasoning_content set."""
    s = make_settings()
    resp = _fake_resp(
        {
            "choices": [
                {
                    "message": {"content": None, "reasoning_content": "thinking..."},
                    "finish_reason": "length",
                }
            ]
        }
    )
    with patch.object(llm, "_post_with_retry", return_value=resp):
        with pytest.raises(ValueError, match="empty content"):
            llm.call_llm("sys", "user", s, count=21)


def test_reasoning_enabled_true_grows_budget_additively():
    """max_tokens must be base_budget + reasoning_max_tokens, never a flat override."""
    s = make_settings(reasoning_enabled=True, reasoning_effort="low", reasoning_max_tokens=8192)
    captured = {}

    def fake_post(url, payload, headers):
        captured["payload"] = payload
        return _fake_resp({"choices": [{"message": {"content": "[]"}, "finish_reason": "stop"}]})

    with patch.object(llm, "_post_with_retry", side_effect=fake_post):
        llm.call_llm("sys", "user", s, count=21)

    base_budget = llm._token_budget(21)
    assert captured["payload"]["max_tokens"] == base_budget + 8192
    assert captured["payload"]["chat_template_kwargs"] == {"reasoning_effort": "low"}


def test_reasoning_enabled_false_forces_effort_none():
    s = make_settings(reasoning_enabled=False)
    captured = {}

    def fake_post(url, payload, headers):
        captured["payload"] = payload
        return _fake_resp({"choices": [{"message": {"content": "[]"}, "finish_reason": "stop"}]})

    with patch.object(llm, "_post_with_retry", side_effect=fake_post):
        llm.call_llm("sys", "user", s, count=21)

    assert captured["payload"]["chat_template_kwargs"] == {"reasoning_effort": "none"}


def test_reasoning_enabled_none_omits_field_entirely():
    """The critical backward-compat case: key absent from config.yaml -> payload untouched."""
    s = make_settings(reasoning_enabled=None)
    captured = {}

    def fake_post(url, payload, headers):
        captured["payload"] = payload
        return _fake_resp({"choices": [{"message": {"content": "[]"}, "finish_reason": "stop"}]})

    with patch.object(llm, "_post_with_retry", side_effect=fake_post):
        llm.call_llm("sys", "user", s, count=21)

    assert "chat_template_kwargs" not in captured["payload"]
    assert captured["payload"]["max_tokens"] == llm._token_budget(21)
