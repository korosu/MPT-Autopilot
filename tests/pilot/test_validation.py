"""
Tests for the whitelists `_validate_against_config` applies to LLM output.

The lists mirror MoneyPrinterTurbo's own handling: an LLM that invents a
plausible-looking value must not have it written into jobs.yaml, because the
render only fails later, after the job has been queued.
"""

from __future__ import annotations

from mpt_autopilot.pilot.refill import (
    _VALID_ANIMATIONS,
    _VALID_BGM_TYPES,
    _VALID_CONCAT_MODES,
    _VALID_DISPLAY_MODES,
    _VALID_FIT_MODES,
    _VALID_TRANSITION_MODES,
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


# ── New upstream field validation (MPT v1.3.6) ───────────────────────────────


def test_valid_fit_modes_cover_contain():
    assert _VALID_FIT_MODES == {"cover", "contain"}


def test_valid_transition_modes():
    assert _VALID_TRANSITION_MODES == {
        "None",
        "Shuffle",
        "FadeIn",
        "FadeOut",
        "SlideIn",
        "SlideOut",
        "ZoomIn",
        "ZoomOut",
    }


def test_valid_display_modes():
    assert _VALID_DISPLAY_MODES == {"sentence", "word_by_word"}


def test_valid_animations():
    assert _VALID_ANIMATIONS == {"none", "pop_spring"}


def test_cover_fit_mode_survives():
    result = _validate_against_config({"video_subject": "x", "video_fit_mode": "cover"}, _lang())
    assert result["video_fit_mode"] == "cover"


def test_contain_fit_mode_survives():
    result = _validate_against_config({"video_subject": "x", "video_fit_mode": "contain"}, _lang())
    assert result["video_fit_mode"] == "contain"


def test_invented_fit_mode_falls_back():
    result = _validate_against_config({"video_subject": "x", "video_fit_mode": "stretch"}, _lang())
    assert result["video_fit_mode"] == "cover"


def test_subtitle_display_mode_survives():
    result = _validate_against_config(
        {"video_subject": "x", "subtitle_display_mode": "word_by_word"}, _lang()
    )
    assert result["subtitle_display_mode"] == "word_by_word"


def test_invented_subtitle_display_mode_falls_back():
    result = _validate_against_config(
        {"video_subject": "x", "subtitle_display_mode": "char_by_char"}, _lang()
    )
    assert result["subtitle_display_mode"] == "sentence"


def test_subtitle_animation_pop_spring_survives():
    result = _validate_against_config(
        {"video_subject": "x", "subtitle_animation": "pop_spring"}, _lang()
    )
    assert result["subtitle_animation"] == "pop_spring"


def test_video_clip_speed_valid_and_invalid():
    result = _validate_against_config({"video_subject": "x", "video_clip_speed": 1.5}, _lang())
    assert result["video_clip_speed"] == 1.5
    result = _validate_against_config({"video_subject": "x", "video_clip_speed": 0}, _lang())
    assert result["video_clip_speed"] == 1.0


def test_video_music_prompt_passthrough():
    result = _validate_against_config(
        {"video_subject": "x", "video_music_prompt": "upbeat electronic"}, _lang()
    )
    assert result["video_music_prompt"] == "upbeat electronic"


def test_rounded_subtitle_background_bool_coerce():
    result = _validate_against_config(
        {"video_subject": "x", "rounded_subtitle_background": 1}, _lang()
    )
    assert result["rounded_subtitle_background"] is True
    result = _validate_against_config(
        {"video_subject": "x", "rounded_subtitle_background": 0}, _lang()
    )
    assert result["rounded_subtitle_background"] is False


# -- P3: defensive validation for previously-unvalidated fields ---


def test_custom_system_prompt_honours_short_string():
    result = _validate_against_config(
        {"video_subject": "x", "custom_system_prompt": "Stay in character"},
        _lang(),
    )
    assert result["custom_system_prompt"] == "Stay in character"


def test_custom_system_prompt_empty_on_too_long():
    long_prompt = "x" * 8001
    result = _validate_against_config(
        {"video_subject": "x", "custom_system_prompt": long_prompt}, _lang()
    )
    assert result["custom_system_prompt"] == ""


def test_custom_system_prompt_type_coerce():
    result = _validate_against_config({"video_subject": "x", "custom_system_prompt": 42}, _lang())
    assert result["custom_system_prompt"] == ""


def test_video_script_honours_short_string():
    result = _validate_against_config(
        {"video_subject": "x", "video_script": "Tell a joke"}, _lang()
    )
    assert result["video_script"] == "Tell a joke"


def test_video_script_empty_on_too_long():
    long_script = "x" * 8001
    result = _validate_against_config({"video_subject": "x", "video_script": long_script}, _lang())
    assert result["video_script"] == ""


def test_video_script_non_string_falls_back():
    result = _validate_against_config({"video_subject": "x", "video_script": 123}, _lang())
    assert result["video_script"] == ""


def test_video_materials_list_of_dicts_survives():
    materials = [
        {"provider": "pexels", "url": "https://example.com/a.mp4", "duration": 10},
    ]
    result = _validate_against_config({"video_subject": "x", "video_materials": materials}, _lang())
    assert result["video_materials"] == materials


def test_video_materials_non_list_falls_back():
    result = _validate_against_config(
        {"video_subject": "x", "video_materials": "not-a-list"}, _lang()
    )
    assert result["video_materials"] == []


def test_video_materials_malformed_items_dropped():
    materials = [
        {"url": "no-provider"},  # missing provider -> dropped
        "not-a-dict",  # wrong type -> dropped
        {"provider": "pexels"},  # valid
    ]
    result = _validate_against_config({"video_subject": "x", "video_materials": materials}, _lang())
    assert result["video_materials"] == [{"provider": "pexels", "url": "", "duration": 0}]
