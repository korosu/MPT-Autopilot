"""
Tests for the whitelists `_validate_against_config` applies to LLM output.

The lists mirror MoneyPrinterTurbo's own handling: an LLM that invents a
plausible-looking value must not have it written into jobs.yaml, because the
render only fails later, after the job has been queued.
"""

from __future__ import annotations

from mpt_autopilot.pilot.refill import (
    _VALID_BGM_TYPES,
    _VALID_CONCAT_MODES,
    _validate_against_config,
)
from mpt_autopilot.pilot.settings import LangSettings

DEFAULTS = {
    "video_clip_duration": 3,
    "video_concat_mode": "random",
    "bgm_type": "random",
    "bgm_volume": 0.15,
    "paragraph_number": 2,
}


def _lang(**overrides) -> LangSettings:
    defaults = dict(DEFAULTS)
    defaults.update(overrides.pop("job_defaults", {}))
    return LangSettings(
        label="English",
        file_suffix="",
        voice_rate_min=1.05,
        voice_rate_max=1.20,
        voices=["gemini:puck", "gemini:kore"],
        job_defaults=defaults,
        theme_list=[],
        **overrides,
    )


def test_concat_modes_match_moneyprinterturbos_enum():
    # MPT's VideoConcatMode has exactly these two members; anything else is a
    # different parameter (transitions) or does not exist.
    assert _VALID_CONCAT_MODES == {"random", "sequential"}


def test_bgm_types_match_what_get_bgm_file_honours():
    # "custom" is what `mpt batch --list-bgm` tells the user to write, so it has
    # to survive validation.
    assert _VALID_BGM_TYPES == {"random", "custom", "none"}


def test_sequential_concat_mode_survives():
    result = _validate_against_config(
        {"video_subject": "x", "video_concat_mode": "sequential"}, _lang()
    )
    assert result["video_concat_mode"] == "sequential"


def test_invented_concat_mode_falls_back_to_the_configured_default():
    result = _validate_against_config(
        {"video_subject": "x", "video_concat_mode": "kaleidoscope"}, _lang()
    )
    assert result["video_concat_mode"] == "random"


def test_custom_bgm_type_survives():
    """A job pinning a specific song uses bgm_type: custom + bgm_file."""
    result = _validate_against_config({"video_subject": "x", "bgm_type": "custom"}, _lang())
    assert result["bgm_type"] == "custom"


def test_none_bgm_type_survives():
    result = _validate_against_config({"video_subject": "x", "bgm_type": "none"}, _lang())
    assert result["bgm_type"] == "none"


def test_invented_bgm_type_falls_back_to_the_configured_default():
    result = _validate_against_config({"video_subject": "x", "bgm_type": "builtin"}, _lang())
    assert result["bgm_type"] == "random"


def test_unlisted_voice_falls_back_to_the_first_configured_one():
    result = _validate_against_config(
        {"video_subject": "x", "voice_name": "gemini:nonexistent"}, _lang()
    )
    assert result["voice_name"] == "gemini:puck"


def test_out_of_range_voice_rate_falls_back_to_the_minimum():
    result = _validate_against_config({"video_subject": "x", "voice_rate": 3.0}, _lang())
    assert result["voice_rate"] == 1.05


def test_boolean_is_not_accepted_as_a_number():
    """bool is a subclass of int, so the isinstance checks exclude it explicitly."""
    result = _validate_against_config(
        {"video_subject": "x", "video_clip_duration": True, "paragraph_number": True}, _lang()
    )
    assert result["video_clip_duration"] == 3
    assert result["paragraph_number"] == 2
