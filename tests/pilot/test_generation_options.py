#!/usr/bin/env python3
"""
test_generation_options.py — pilot.generation's newer knobs (R1-step2/A-3, R4).

  * visual_terms: off by default; on without rules is a named error; on with
    rules injects them into the idea prompt (same LLM call) and the example
    grows a video_terms array.
  * temperature: tri-state like reasoning_enabled — absent = key not sent at
    all, a number = sent verbatim to both transports.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from mpt_autopilot.pilot import llm
from mpt_autopilot.pilot.prompt import build
from mpt_autopilot.pilot.settings import LangSettings, Settings
from mpt_autopilot.pilot.settings import load as load_settings


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        api_key="k",
        base_url="https://selfhosted.example/v1",
        model="m",
        telegram_token="",
        telegram_chat_id="",
        telegram_prefix="p",
        generate_count=21,
        refill_threshold=10,
        scan_dirs=[],
        langs={},
        jobs_dir=None,
        seen_dir=None,
        seen_max_mb=64,
    )
    base.update(overrides)
    return Settings(**base)


def _lang() -> LangSettings:
    return LangSettings(
        label="English",
        file_suffix="",
        voice_rate_min=1.05,
        voice_rate_max=1.2,
        voices=["gemini:puck"],
        job_defaults={},
        theme_list=[],
    )


def _write_config(tmp_path: Path, generation_body: str) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(
        "pilot:\n"
        "  generation:\n"
        f"{generation_body}"
        "langs:\n"
        "  en:\n"
        "    label: English\n"
        '    file_suffix: ""\n',
        encoding="utf-8",
    )
    return config


def _load(monkeypatch, config: Path):
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.test")
    monkeypatch.setenv("LLM_MODEL", "m")
    return load_settings(config, env_path=config)


# ── visual_terms (R1-step2/A-3) ──────────────────────────────────────────────


def test_visual_terms_off_by_default(monkeypatch, tmp_path):
    config = _write_config(tmp_path, "    count: 1\n")
    s = _load(monkeypatch, config)
    assert s.visual_terms_enabled is False
    assert s.visual_terms_rules == ""


def test_visual_terms_on_requires_rules(monkeypatch, tmp_path):
    config = _write_config(tmp_path, "    visual_terms: true\n")
    with pytest.raises(ValueError, match="visual_terms_rules is required"):
        _load(monkeypatch, config)


def test_visual_terms_rules_load(monkeypatch, tmp_path):
    config = _write_config(
        tmp_path, "    visual_terms: true\n    visual_terms_rules: |\n      10-14 English terms.\n"
    )
    s = _load(monkeypatch, config)
    assert s.visual_terms_enabled is True
    assert "10-14 English terms" in s.visual_terms_rules


def test_build_without_rules_keeps_the_prompt_unchanged():
    _, user_plain = build(_lang(), [], 3)
    _, user_rules = build(_lang(), [], 3, visual_terms_rules=None)
    assert user_plain == user_rules
    assert "video_terms" not in user_plain


def test_build_with_rules_asks_for_video_terms():
    rules = "10-14 keywords, always English, concrete subjects."
    _, user = build(_lang(), [], 3, visual_terms_rules=rules)
    assert rules in user
    assert "video_terms" in user
    # The example shows the shape the model should answer with.
    assert '"video_terms"' in user


# ── temperature (R4) ──────────────────────────────────────────────────────────


def _fake_resp(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = lambda: None
    resp.json.return_value = payload
    return resp


def test_temperature_absent_means_key_not_sent():
    s = make_settings()  # temperature defaults to None
    resp = _fake_resp({"choices": [{"message": {"content": "[]"}}]})
    with patch.object(llm, "_post_with_retry", return_value=resp) as post:
        llm._call_openai_compat("sys", "usr", s, 1000)
    assert "temperature" not in post.call_args.args[1]


def test_temperature_number_is_sent_verbatim():
    s = make_settings(temperature=0.9)
    resp = _fake_resp({"choices": [{"message": {"content": "[]"}}]})
    with patch.object(llm, "_post_with_retry", return_value=resp) as post:
        llm._call_openai_compat("sys", "usr", s, 1000)
    assert post.call_args.args[1]["temperature"] == 0.9


def test_temperature_reaches_the_anthropic_transport_too():
    s = make_settings(base_url="https://anthropic.com/v1", api_key="k", temperature=0.5)
    resp = _fake_resp({"content": [{"type": "text", "text": "[]"}]})
    with patch.object(llm, "_post_with_retry", return_value=resp) as post:
        llm._call_anthropic("sys", "usr", s, 1000)
    assert post.call_args.args[1]["temperature"] == 0.5


def test_temperature_config_rejects_non_numbers(monkeypatch, tmp_path):
    config = _write_config(tmp_path, "    temperature: hot\n")
    with pytest.raises(ValueError, match="temperature"):
        _load(monkeypatch, config)


# ── strict_json (R4) ──────────────────────────────────────────────────────────


def test_strict_json_off_by_default():
    s = make_settings()
    resp = _fake_resp({"choices": [{"message": {"content": "[]"}}]})
    with patch.object(llm, "_post_with_retry", return_value=resp) as post:
        llm._call_openai_compat("sys", "usr", s, 1000)
    assert "response_format" not in post.call_args.args[1]


def test_strict_json_sends_response_format():
    s = make_settings(strict_json=True)
    resp = _fake_resp({"choices": [{"message": {"content": "[]"}}]})
    with patch.object(llm, "_post_with_retry", return_value=resp) as post:
        llm._call_openai_compat("sys", "usr", s, 1000)
    assert post.call_args.args[1]["response_format"] == {"type": "json_object"}


# ── topic dedup context (R4: ideas, not slugs) ────────────────────────────────


def test_existing_subjects_by_output_prefers_the_idea_text():
    from mpt_autopilot.pilot import jobs

    cfg = {
        "jobs": [
            {"output_file": "a.mp4", "video_subject": "Octopuses have three hearts"},
            {"output_file": "b.mp4", "video_subject": "   "},  # blank → falls back to slug
            {"output_file": "c.mp4"},  # no subject at all
            {"video_subject": "orphan without output"},
        ]
    }
    subjects = jobs.existing_subjects_by_output(cfg)
    assert subjects == {"a.mp4": "Octopuses have three hearts"}
    # b/c are absent — the caller falls back to the filename for them.


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
