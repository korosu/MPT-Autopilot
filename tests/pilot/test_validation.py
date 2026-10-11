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
    # MPT's VideoTransitionMode enum members, minus the "none" case: that one
    # carries Python None (JSON null), and the schema field defaults to null —
    # so "no transition" is the ABSENCE of the key, never the string "None".
    assert _VALID_TRANSITION_MODES == {
        "Shuffle",
        "FadeIn",
        "FadeOut",
        "SlideIn",
        "SlideOut",
        "ZoomIn",
        "ZoomOut",
    }


def test_string_none_transition_is_removed_not_substituted():
    """B-1: the string "None" is not an enum member — pydantic answers 400,
    which retried 3×180s and killed the batch. The key must be dropped."""
    result = _validate_against_config(
        {"video_subject": "x", "video_transition_mode": "None"}, _lang()
    )
    assert "video_transition_mode" not in result


def test_absent_transition_stays_absent():
    result = _validate_against_config({"video_subject": "x"}, _lang())
    assert "video_transition_mode" not in result


def test_real_transition_survives():
    result = _validate_against_config(
        {"video_subject": "x", "video_transition_mode": "FadeIn"}, _lang()
    )
    assert result["video_transition_mode"] == "FadeIn"


def test_bad_transition_falls_back_to_configured_one():
    lang = _lang(job_defaults={"video_transition_mode": "ZoomIn"})
    result = _validate_against_config(
        {"video_subject": "x", "video_transition_mode": "kaleidoscope"}, lang
    )
    assert result["video_transition_mode"] == "ZoomIn"


def test_ghost_subtitle_background_fields_are_not_added():
    """B-2: VideoParams has no subtitle_background_enabled/color — validation
    must not write them, and input copies of them must not survive either."""
    result = _validate_against_config(
        {
            "video_subject": "x",
            "subtitle_background_enabled": True,
            "subtitle_background_color": "#111111",
        },
        _lang(),
    )
    assert "subtitle_background_enabled" not in result
    assert "subtitle_background_color" not in result


def test_video_terms_string_is_normalised_to_a_list():
    result = _validate_against_config(
        {"video_subject": "x", "video_terms": "octopus hearts, , underwater"}, _lang()
    )
    assert result["video_terms"] == ["octopus hearts", "underwater"]


def test_video_terms_list_is_cleaned():
    result = _validate_against_config(
        {"video_subject": "x", "video_terms": ["lab pipette", "", 42, "gloved hand"]}, _lang()
    )
    assert result["video_terms"] == ["lab pipette", "gloved hand"]


def test_empty_video_terms_key_is_dropped():
    result = _validate_against_config({"video_subject": "x", "video_terms": ""}, _lang())
    assert "video_terms" not in result


def test_match_materials_to_script_is_coerced_to_bool():
    result = _validate_against_config(
        {"video_subject": "x", "match_materials_to_script": 1}, _lang()
    )
    assert result["match_materials_to_script"] is True
    result = _validate_against_config({"video_subject": "x"}, _lang())
    assert result["match_materials_to_script"] is False


def test_video_count_garbage_is_dropped():
    result = _validate_against_config({"video_subject": "x", "video_count": 0}, _lang())
    assert "video_count" not in result
    result = _validate_against_config({"video_subject": "x", "video_count": "two"}, _lang())
    assert "video_count" not in result


def test_video_count_valid_is_kept():
    result = _validate_against_config({"video_subject": "x", "video_count": 1}, _lang())
    assert result["video_count"] == 1


def test_voice_volume_garbage_is_dropped():
    result = _validate_against_config({"video_subject": "x", "voice_volume": "loud"}, _lang())
    assert "voice_volume" not in result
    result = _validate_against_config({"video_subject": "x", "voice_volume": True}, _lang())
    assert "voice_volume" not in result


def test_voice_volume_valid_is_kept():
    result = _validate_against_config({"video_subject": "x", "voice_volume": 0.8}, _lang())
    assert result["voice_volume"] == 0.8


def test_user_video_script_prompt_is_appended_to_not_replaced():
    """B-5: job_defaults.video_script_prompt was overwritten wholesale, killing
    the only reachable lever over the script text."""
    lang = _lang(job_defaults={"video_script_prompt": "Always end with a question."})
    result = _validate_against_config({"video_subject": "x"}, lang)
    assert result["video_script_prompt"].startswith("Always end with a question.")
    assert "hook" in result["video_script_prompt"]  # built instruction appended


def test_script_prompt_has_no_pause_tags():
    """B-4: [pause:Xs] tags are stripped by MPT before TTS — the instruction
    only burned the 2000-char budget."""
    result = _validate_against_config(
        {"video_subject": "x"}, _lang(job_defaults={"duration_range": "30-45"})
    )
    assert "[pause" not in result["video_script_prompt"]


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
