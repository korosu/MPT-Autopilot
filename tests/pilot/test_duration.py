#!/usr/bin/env python3
"""
Self-check for duration_range feature.

Run: uv run python tests/test_duration.py

Uses only stdlib + the package, no test framework.
"""

import sys
from pathlib import Path

# Add src to path
src_dir = Path(__file__).parent.parent / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from shorts_pilot.refill import (
    parse_duration_range,
    duration_to_words,
    paragraph_floor,
    build_duration_instruction,
    _validate_against_config,
    WORDS_PER_SECOND,
)
from shorts_pilot.generator.settings import LangSettings


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
    )

    job = {"video_subject": "Test fact about octopuses", "output_file": "test_octopuses.mp4"}
    result = _validate_against_config(job, lang_cfg)

    assert "video_script_prompt" in result, "should have video_script_prompt"
    assert "between 216 and 286 words" in result["video_script_prompt"], \
        f"instruction should mention 216-286 words, got: {result['video_script_prompt']}"
    assert result["paragraph_number"] == 3, "max(2, floor(90-120)) = 3"

    print("[check] validation with duration_range: OK")


def test_validation_without_duration_range() -> None:
    """Verify backward compat: no duration_range means no video_script_prompt."""
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
    )

    job = {"video_subject": "Test fact", "output_file": "test.mp4"}
    result = _validate_against_config(job, lang_cfg)

    assert "video_script_prompt" not in result, "should NOT have video_script_prompt when not configured"

    # Test LLM hallucination defense: if LLM returns video_script_prompt anyway
    job_with_hallucination = {
        "video_subject": "Test fact",
        "output_file": "test.mp4",
        "video_script_prompt": "LLM made this up",  # should be stripped
    }
    result2 = _validate_against_config(job_with_hallucination, lang_cfg)
    assert "video_script_prompt" not in result2, "LLM hallucinated video_script_prompt should be removed"

    print("[check] validation without duration_range (backward compat): OK")


def main() -> None:
    test_words_per_second()
    test_parse_duration_range()
    test_duration_to_words()
    test_paragraph_floor()
    test_build_duration_instruction()
    test_validation_with_duration_range()
    test_validation_without_duration_range()
    print("test_duration: OK")


if __name__ == "__main__":
    main()