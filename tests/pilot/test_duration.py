#!/usr/bin/env python3
"""
test_duration.py — tests for duration_range feature.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add src to path
src_dir = Path(__file__).parent.parent / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from mpt_autopilot.pilot.refill import (  # noqa: E402
    WORDS_PER_SECOND,
    _validate_against_config,
    build_duration_instruction,
    duration_to_words,
    paragraph_floor,
    parse_duration_range,
)
from mpt_autopilot.pilot.settings import LangSettings  # noqa: E402


def test_words_per_second() -> None:
    """Verify calibration constant."""
    assert WORDS_PER_SECOND == 2.39, f"WORDS_PER_SECOND should be 2.39, got {WORDS_PER_SECOND}"
    print("[check] WORDS_PER_SECOND: OK")


def test_parse_duration_range() -> None:
    """Verify duration_range parsing."""
    # Valid formats
    assert parse_duration_range("30-60") == (30, 60), "30-60 should parse to (30,60)"
    assert parse_duration_range("90-120") == (90, 120), "90-120 should parse to (90,120)"
    assert parse_duration_range("120+") == (120, None), "120+ should parse to (120,None)"

    # Empty/None -> None (no limit)
    assert parse_duration_range("") is None, "empty string -> None"
    assert parse_duration_range(None) is None, "None -> None"

    # Invalid formats
    try:
        parse_duration_range("60-30")
        assert False, "60-30 should raise (min > max)"
    except ValueError:
        pass

    try:
        parse_duration_range("abc")
        assert False, "abc should raise (invalid format)"
    except ValueError:
        pass

    try:
        parse_duration_range("30")
        assert False, "30 should raise (missing - or +)"
    except ValueError:
        pass

    print("[check] parse_duration_range: OK")


def test_duration_to_words() -> None:
    """Verify seconds-to-words conversion with math.ceil/floor."""
    # 30-60 sec -> words
    lo, hi = duration_to_words((30, 60))
    assert lo == 72, f"ceil(30*2.39) = 72, got {lo}"  # 71.7 -> 72
    assert hi == 143, f"floor(60*2.39) = 143, got {hi}"  # 143.4 -> 143

    # 120+ sec -> no upper bound
    lo, hi = duration_to_words((120, None))
    assert lo == 287, f"ceil(120*2.39) = 287, got {lo}"  # 286.8 -> 287
    assert hi is None, "upper bound should remain None for open-ended"

    print("[check] duration_to_words: OK")


def test_paragraph_floor() -> None:
    """Verify paragraph_number floor based on duration band."""
    assert paragraph_floor((30, 60)) == 1, "30-60 sec -> 1 paragraph"
    assert paragraph_floor((60, 90)) == 2, "60-90 sec -> 2 paragraphs"
    assert paragraph_floor((90, 120)) == 3, "90-120 sec -> 3 paragraphs"
    assert paragraph_floor((120, None)) == 3, "120+ sec -> 3 paragraphs"

    print("[check] paragraph_floor: OK")


def test_build_duration_instruction() -> None:
    """Verify instruction strings are correctly formatted."""
    instruction = build_duration_instruction((72, 143))
    assert "between 72 and 143 words" in instruction, f"missing range in: {instruction}"
    assert "Do not exceed" in instruction, "missing 'Do not exceed'"
    assert "do not fall below" in instruction, "missing 'do not fall below'"

    instruction_open = build_duration_instruction((287, None))  # for "120+" case
    assert "at least 287 words" in instruction_open, f"missing 'at least' in: {instruction_open}"
    assert "Do not pad artificially" in instruction_open, "missing 'Do not pad'"

    print("[check] build_duration_instruction: OK")


def test_validation_with_duration_range() -> None:
    """Verify _validate_against_config injects video_script_prompt when duration_range set."""
    lang_cfg = LangSettings(
        label="English",
        file_suffix="",
        voice_rate_min=1.05,
        voice_rate_max=1.20,
        voices=["gemini:puck"],
        job_defaults={
            "video_clip_duration": 3,
            "video_concat_mode": "random",
            "bgm_type": "random",
            "bgm_volume": 0.15,
            "paragraph_number": 2,
            "duration_range": "90-120",
        },
        theme_list=[],
    )

    job = {"video_subject": "Test fact about octopuses", "output_file": "test_octopuses.mp4"}
    result = _validate_against_config(job, lang_cfg)

    assert "video_script_prompt" in result, "should have video_script_prompt"
    assert "between 216 and 286 words" in result["video_script_prompt"], (
        f"instruction should mention 216-286 words, got: {result['video_script_prompt']}"
    )
    assert result["paragraph_number"] == 3, "max(2, floor(90-120)) = 3"

    print("[check] validation with duration_range: OK")


def test_validation_without_duration_range() -> None:
    """Verify always-on hook: video_script_prompt is present even without duration_range."""
    lang_cfg = LangSettings(
        label="English",
        file_suffix="",
        voice_rate_min=1.05,
        voice_rate_max=1.20,
        voices=["gemini:puck"],
        job_defaults={
            "video_clip_duration": 3,
            "video_concat_mode": "random",
            "bgm_type": "random",
            "bgm_volume": 0.15,
            "paragraph_number": 2,
            # No duration_range
        },
        theme_list=[],
    )

    job = {"video_subject": "Test fact", "output_file": "test.mp4"}
    result = _validate_against_config(job, lang_cfg)

    # Always-on hook: video_script_prompt is always present
    assert "video_script_prompt" in result, (
        "should have video_script_prompt (always-on hook) even without duration_range"
    )
    assert "hook" in result["video_script_prompt"].lower(), (
        f"instruction should contain hook wording, got: {result['video_script_prompt']}"
    )
    # No word-count guidance when duration_range is absent
    assert "words" not in result["video_script_prompt"], (
        "should NOT have word-count guidance when duration_range not set"
    )

    # Hook is always ours — defense against LLM hallucination
    job_with_hallucination = {
        "video_subject": "Test fact",
        "output_file": "test.mp4",
        "video_script_prompt": "LLM made this up",
    }
    result2 = _validate_against_config(job_with_hallucination, lang_cfg)
    # Our hook overwrites any LLM hallucination
    assert "hook" in result2["video_script_prompt"].lower(), (
        "our hook should replace LLM hallucination"
    )

    print("[check] validation without duration_range (always-on hook): OK")


# ── --duration-range CLI flag ─────────────────────────────────────────────────


def test_flag_overrides_config_duration_range() -> None:
    """run(duration_range=...) must beat langs.<code>.job_defaults.duration_range.

    The flag rebases lang_cfg, so the system prompt's duration phrase and the
    word-count instruction must both reflect 120+ (not the config's 30-60).
    """
    import json
    import tempfile
    from unittest.mock import patch

    import mpt_autopilot.pilot.refill as refill
    from mpt_autopilot.pilot.settings import LangSettings, Settings

    def make_settings() -> Settings:
        return Settings(
            api_key="",
            base_url="",
            model="",
            telegram_token="",
            telegram_chat_id="",
            telegram_prefix="shorts-pilot",
            generate_count=3,
            refill_threshold=10,
            scan_dirs=[],
            langs={
                "en": LangSettings(
                    label="English",
                    file_suffix="",
                    voice_rate_min=1.05,
                    voice_rate_max=1.20,
                    voices=["gemini:puck"],
                    job_defaults={
                        "video_clip_duration": 3,
                        "video_concat_mode": "random",
                        "bgm_type": "random",
                        "bgm_volume": 0.15,
                        "paragraph_number": 2,
                        "duration_range": "30-60",  # config says short videos
                    },
                    theme_list=[],
                )
            },
            jobs_dir=None,
            seen_dir=None,
            seen_max_mb=64,
        )

    captured: dict = {}

    def mock_call_llm(system_prompt, user_prompt, settings, count):
        captured["system_prompt"] = system_prompt
        return json.dumps(
            [
                {
                    "output_file": "fact_flag_a.mp4",
                    "video_subject": "Octopuses have three hearts pumping blue blood everywhere ",
                }
            ]
        )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        (tmp_dir / "jobs.yaml").write_text("jobs:\n", encoding="utf-8")
        with patch.object(refill, "load_settings", return_value=make_settings()):
            with patch.object(refill, "call_llm", mock_call_llm):
                added = refill.run(
                    lang="en",
                    jobs_dir=tmp_dir,
                    seen_dir=tmp_dir,
                    force=True,
                    count_override=1,
                    threshold_override=10,
                    topics=None,
                    themes=None,
                    duration_range="120+",
                )

        assert added == 1, f"expected 1 job added, got {added}"
        # System prompt claims the flag's range, not the config's 30-60.
        assert "120" in captured["system_prompt"], (
            f"system prompt should mention 120+ seconds, got: {captured['system_prompt']}"
        )
        assert "30" not in captured["system_prompt"], (
            f"system prompt should NOT mention the config's 30-60, got: {captured['system_prompt']}"
        )

    print("[check] flag overrides config duration_range: OK")


def test_execute_rejects_invalid_flag_before_llm() -> None:
    """execute() must exit 1 on a bad --duration-range without calling the LLM."""
    import argparse
    import tempfile
    from unittest.mock import patch

    import mpt_autopilot.pilot.refill as refill

    def parser() -> argparse.ArgumentParser:
        p = argparse.ArgumentParser()
        refill.add_arguments(p)
        return p

    args = parser().parse_args(["--lang", "en", "--force", "--duration-range", "60-30"])

    called = {"llm": False}

    def mock_call_llm(system_prompt, user_prompt, settings, count):
        called["llm"] = True
        return "[]"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        (tmp_dir / "jobs.yaml").write_text("jobs:\n", encoding="utf-8")
        with patch.object(refill, "load_settings", return_value=None):
            with patch.object(refill, "call_llm", mock_call_llm):
                code = refill.execute(args, config_path=tmp_dir / "absent.yaml")

    assert code == 1, f"expected exit 1, got {code}"
    assert called["llm"] is False, "LLM must not be called for an invalid flag"

    print("[check] execute rejects invalid flag before LLM: OK")


def test_topics_mode_applies_flag() -> None:
    """Topics mode has no LLM, but the flag still steers _validate_against_config."""
    import tempfile
    from unittest.mock import patch

    import yaml

    import mpt_autopilot.pilot.refill as refill
    from mpt_autopilot.pilot.settings import LangSettings, Settings

    def make_settings() -> Settings:
        return Settings(
            api_key="",
            base_url="",
            model="",
            telegram_token="",
            telegram_chat_id="",
            telegram_prefix="shorts-pilot",
            generate_count=3,
            refill_threshold=10,
            scan_dirs=[],
            langs={
                "en": LangSettings(
                    label="English",
                    file_suffix="",
                    voice_rate_min=1.05,
                    voice_rate_max=1.20,
                    voices=["gemini:puck"],
                    job_defaults={
                        "video_clip_duration": 3,
                        "video_concat_mode": "random",
                        "bgm_type": "random",
                        "bgm_volume": 0.15,
                        "paragraph_number": 2,
                        # No duration_range in config — the flag is the only source.
                    },
                    theme_list=[],
                )
            },
            jobs_dir=None,
            seen_dir=None,
            seen_max_mb=64,
        )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        (tmp_dir / "jobs.yaml").write_text("jobs:\n", encoding="utf-8")
        with patch.object(refill, "load_settings", return_value=make_settings()):
            added = refill.run(
                lang="en",
                jobs_dir=tmp_dir,
                seen_dir=tmp_dir,
                force=False,
                count_override=None,
                threshold_override=None,
                topics=["The tongue is not the strongest muscle"],
                themes=None,
                duration_range="90-120",
            )

        assert added == 1, f"expected 1 topic job added, got {added}"
        # The written jobs.yaml must carry the flag's word-count guidance.
        data = yaml.safe_load((tmp_dir / "jobs.yaml").read_text(encoding="utf-8"))
        prompt = data["jobs"][0]["video_script_prompt"]
        assert "between 216 and 286 words" in prompt, (
            f"90-120s must map to 216-286 words, got: {prompt}"
        )
        assert data["jobs"][0]["paragraph_number"] == 3, "floor(90-120) = 3"

    print("[check] topics mode applies flag: OK")


def main() -> None:
    test_words_per_second()
    test_parse_duration_range()
    test_duration_to_words()
    test_paragraph_floor()
    test_build_duration_instruction()
    test_validation_with_duration_range()
    test_validation_without_duration_range()
    test_flag_overrides_config_duration_range()
    test_execute_rejects_invalid_flag_before_llm()
    test_topics_mode_applies_flag()
    print("test_duration: OK")


if __name__ == "__main__":
    main()
