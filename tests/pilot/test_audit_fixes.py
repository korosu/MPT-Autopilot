"""
Regression tests for the 2026-10-02 pilot audit findings (M-11…M-18).

Each test pins the contract the finding said was unenforced, so the prompt's
promises, the config fallbacks and the suffix filter can't drift apart again.
"""

from __future__ import annotations

import pytest
import yaml

from mpt_autopilot.pilot.init_seen import _filter_by_suffix
from mpt_autopilot.pilot.jobs import (
    NAME_MAX_CHARS,
    _scalar,
    existing_job_names_from,
    existing_names_from,
)
from mpt_autopilot.pilot.llm import parse_json_array
from mpt_autopilot.pilot.prompt import THEME_MAX_SUBJECT_CHARS
from mpt_autopilot.pilot.prompt import build as build_prompt
from mpt_autopilot.pilot.refill import (
    _SHORT_MIN_SUBJECT_CHARS,
    _deduplicate,
    _validate_against_config,
)
from mpt_autopilot.pilot.settings import LangSettings, Settings


def _lang(**job_defaults) -> LangSettings:
    return LangSettings(
        label="English",
        file_suffix="",
        voice_rate_min=1.05,
        voice_rate_max=1.20,
        voices=["gemini:puck"],
        job_defaults={"paragraph_number": 2, **job_defaults},
        theme_list=[],
    )


# ── M-11: job_defaults is config, not validated input ────────────────────────


def test_string_paragraph_number_default_does_not_crash_the_band_floor():
    """`paragraph_number: "2"` in config.yaml used to raise TypeError in max()."""
    lang = _lang(paragraph_number="2", duration_range="90-120")

    out = _validate_against_config({"paragraph_number": None}, lang)

    assert out["paragraph_number"] == 3  # paragraph_floor((90, 120)) == 3


def test_non_numeric_bgm_volume_default_falls_back_to_the_builtin():
    out = _validate_against_config({"bgm_volume": "loud"}, _lang(bgm_volume="loud"))

    assert out["bgm_volume"] == 0.15


# ── M-12: theme mode's promised 80-char ceiling is enforced ──────────────────


def test_theme_mode_clamps_video_subject_to_the_limit_the_prompt_promises():
    long_title = "Why " + "very " * 40 + "long"
    assert len(long_title) > THEME_MAX_SUBJECT_CHARS

    (job,) = _deduplicate(
        [{"output_file": "t.mp4", "video_subject": long_title}],
        set(),
        _lang(),
        min_subject_chars=_SHORT_MIN_SUBJECT_CHARS,
        max_subject_chars=THEME_MAX_SUBJECT_CHARS,
    )

    assert len(job["video_subject"]) <= THEME_MAX_SUBJECT_CHARS


def test_prompt_quotes_the_name_limit_that_safe_name_actually_enforces():
    _system, user = build_prompt(_lang(), [], 1)

    assert f"max {NAME_MAX_CHARS} chars" in user


# ── M-13: topics are imported verbatim, short ones included ──────────────────


def test_a_short_topic_is_not_discarded_as_malformed():
    subject = "Octopus hearts"  # 14 chars — under the 30-char script floor
    assert _SHORT_MIN_SUBJECT_CHARS <= len(subject) < 30

    kept = _deduplicate(
        [{"output_file": "octopus_hearts.mp4", "video_subject": subject}],
        set(),
        _lang(),
        min_subject_chars=_SHORT_MIN_SUBJECT_CHARS,
    )

    assert [j["video_subject"] for j in kept] == [subject]


# ── M-14: the system prompt states the configured duration ──────────────────


def test_system_prompt_states_the_configured_duration_range():
    system, _user = build_prompt(_lang(), [], 1, (60, 90))

    assert "60–90 seconds" in system
    assert "30–45 seconds" not in system


def test_system_prompt_keeps_its_default_claim_without_a_configured_range():
    system, _user = build_prompt(_lang(), [], 1)

    assert "30–45 seconds" in system


def test_open_ended_duration_range_reads_as_a_minimum():
    system, _user = build_prompt(_lang(), [], 1, (60, None))

    assert "at least 60 seconds" in system


# ── M-16: artefact stripping applies to the response, not to values ─────────


def test_a_trailing_comma_inside_a_value_survives_parsing():
    raw = '[{"output_file": "a.mp4", "video_subject": "First, second,"}]'

    assert parse_json_array(raw)[0]["video_subject"] == "First, second,"


def test_a_trailing_artefact_after_the_array_is_still_stripped():
    raw = '[{"output_file": "a.mp4", "video_subject": "Fact"}],'

    assert parse_json_array(raw) == [{"output_file": "a.mp4", "video_subject": "Fact"}]


# ── M-18: suffix matching is case-insensitive on both sides ─────────────────


def test_an_uppercase_config_suffix_still_matches_files_on_disk():
    names = {"fact_one_es.mp4", "fact_two.mp4"}

    assert _filter_by_suffix(names, "_ES", {"_ES"}) == {"fact_one_es.mp4"}


def test_the_default_language_excludes_uppercase_configured_suffixes():
    names = {"fact_one_es.mp4", "fact_two.mp4"}

    assert _filter_by_suffix(names, "", {"_ES"}) == {"fact_two.mp4"}


# ── M-17: reasoning_effort is validated like the enricher's ─────────────────


def test_an_unknown_reasoning_effort_is_rejected(monkeypatch, tmp_path):
    from mpt_autopilot.pilot.settings import load as load_settings

    config = tmp_path / "config.yaml"
    config.write_text(
        "pilot:\n"
        "  generation:\n"
        "    reasoning_effort: ludicrous\n"
        "langs:\n"
        "  en:\n"
        "    label: English\n"
        '    file_suffix: ""\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.test")
    monkeypatch.setenv("LLM_MODEL", "m")

    with pytest.raises(ValueError, match="reasoning_effort"):
        load_settings(config, env_path=config)


# ── Pilot L-block: yaml escaping, name sets, provider detection ──────────────


@pytest.mark.parametrize("code", [0x00, 0x1F, 0x7F, 0x80, 0x85, 0x9F])
def test_scalar_escapes_every_character_pyyaml_mishandles(code):
    """
    A C1 character written verbatim either makes the jobs file unreadable
    (0x80-0x84, 0x86-0x9F raise ReaderError) or is silently read back as a
    space (0x85). Escaped, it must round-trip exactly.
    """
    subject = f"x{chr(code)}y"
    loaded = yaml.safe_load(f"a: {_scalar(subject)}")
    assert loaded["a"] == subject


def test_scalar_leaves_nbsp_alone():
    """0xA0 is legal YAML — escaping it would churn every subject containing one."""
    assert _scalar("x\xa0y") == '"x\xa0y"'


def test_existing_names_skips_jobs_without_an_output_file():
    cfg = {"jobs": [{"output_file": "a.mp4"}, {"name": "no_file"}]}
    assert existing_names_from(cfg) == {"a.mp4"}


def test_existing_job_names_are_reserved_against_the_queue():
    """A generated job must not reuse a name already sitting in the jobs file."""
    cfg = {"jobs": [{"name": "octopus_hearts", "output_file": "octopus_hearts.mp4"}]}
    raw = [
        {
            "name": "octopus_hearts",
            "video_subject": "Octopus hearts",
            "output_file": "octopus_hearts_two.mp4",
        }
    ]

    clean = _deduplicate(
        raw,
        set(),
        _lang(),
        min_subject_chars=_SHORT_MIN_SUBJECT_CHARS,
        known_job_names=existing_job_names_from(cfg),
    )

    assert [j["name"] for j in clean] == ["octopus_hearts_2"]


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://api.anthropic.com/v1", True),
        ("https://anthropic.com", True),
        ("https://proxy.example/?upstream=anthropic.com", False),
        ("https://anthropic.com.evil.test/v1", False),
    ],
)
def test_is_anthropic_matches_the_host_not_the_whole_url(base_url, expected):
    settings = Settings(
        api_key="k",
        base_url=base_url,
        model="m",
        telegram_token="",
        telegram_chat_id="",
        telegram_prefix="",
        generate_count=1,
        refill_threshold=1,
        seen_max_mb=64,
        scan_dirs=[],
        langs={},
        jobs_dir=None,
        seen_dir=None,
    )
    assert settings.is_anthropic is expected
