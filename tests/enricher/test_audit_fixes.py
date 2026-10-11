"""Tests for the enricher audit fixes (2026-10): M-33…M-41 and their L items.

Covered here:
  - M-33: batch tags go through the same max_tags/min_tags budget as per-file
  - M-34: --lang reaches the batch prompt and the sidecar language
  - M-37: config coercion (platform/banned_tags/always_include/supports_temperature)
  - M-38: _chat fails with a named error when choices[] is missing
  - M-36/L: writer backs up non-object and unreadable sidecars; keeps prior backups
  - L: validate_and_filter preserves the first occurrence's casing
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mpt_autopilot.enricher.llm import batch_generate_hashtags
from mpt_autopilot.enricher.postprocess import validate_and_filter
from mpt_autopilot.enricher.writer import build_hashtags_block, write_hashtags


def _llm_settings(**overrides):
    """Real-valued settings for llm.py tests (len()/frozenset() of a MagicMock
    would raise before any assertion runs)."""
    base = dict(
        max_tags=5,
        min_tags=3,
        max_tag_length=20,
        banned_tags=frozenset(),
        always_include=[],
        platform="youtube",
        base_url="https://api.test",
        api_key="key",
        model="test-model",
        supports_temperature=True,
        reasoning_enabled=None,
        reasoning_effort="medium",
        reasoning_max_tokens=8192,
        log_file=Path("/tmp/enricher-test.log"),
        max_log_size=100_000,
        prompt_batch=(
            "Hashtags for {count} videos on {platform}. Tags in {language}, "
            "{min_tags}-{max_tags} tags, max {max_tag_length} chars. "
            "Excluded: {excluded_tags}.\nVIDEOS:\n{videos}\n"
            'Respond ONLY as JSON: {{"results": [{{"filename": "...", '
            '"language": "...", "tags": []}}]}}'
        ),
        prompt_title_description=(
            "Title+description for {video_subject} ({language}), platform "
            "{platform}. Script: {script}\n"
            'JSON: {{"title": "...", "description": "..."}}'
        ),
    )
    base.update(overrides)
    return MagicMock(**base)


def _batch_response(*, filename: str = "a.mp4", tags: list[str], language: str = "English"):
    return json.dumps({"results": [{"filename": filename, "language": language, "tags": tags}]})


# ── M-33: enforced budgets in batch mode ──────────────────────────────────────


def test_batch_tags_are_capped_at_max_tags():
    """The prompt's max_tags is only text; the enforced cap is _finalize_tags."""
    from mpt_autopilot.enricher import llm as llm_mod

    ten_tags = [f"#t{i}" for i in range(10)]
    with (
        patch.object(llm_mod, "settings", _llm_settings()),
        patch.object(llm_mod, "_build_excluded_string", return_value=""),
        patch.object(llm_mod, "_chat", return_value=_batch_response(tags=ten_tags)),
    ):
        results = batch_generate_hashtags([("a.mp4", "topic")], "youtube")

    assert results[0]["tags"] == ten_tags[:5]


def test_batch_below_min_tags_logs_a_warning():
    from mpt_autopilot.enricher import llm as llm_mod

    with (
        patch.object(llm_mod, "settings", _llm_settings()),
        patch.object(llm_mod, "_build_excluded_string", return_value=""),
        patch.object(llm_mod, "_chat", return_value=_batch_response(tags=["#ant", "#bee"])),
        patch.object(llm_mod, "_get_log") as mock_log,
    ):
        results = batch_generate_hashtags([("a.mp4", "topic")], "youtube")

    assert len(results[0]["tags"]) == 2  # not fatal, but…
    mock_log().warn.assert_called_once()  # …the shortfall is visible


def test_batch_merges_always_include_like_per_file():
    """Batch and per-file paths must produce the same shape of result."""
    from mpt_autopilot.enricher import llm as llm_mod

    with (
        patch.object(llm_mod, "settings", _llm_settings(always_include=["#shorts"])),
        patch.object(llm_mod, "_build_excluded_string", return_value=""),
        patch.object(llm_mod, "_chat", return_value=_batch_response(tags=["#ant"])),
    ):
        results = batch_generate_hashtags([("a.mp4", "topic")], "youtube")

    assert results[0]["tags"] == ["#shorts", "#ant"]


# ── M-34: --lang reaches the batch prompt ─────────────────────────────────────


def test_batch_prompt_carries_lang_override():
    from mpt_autopilot.enricher import llm as llm_mod

    with (
        patch.object(llm_mod, "settings", _llm_settings()),
        patch.object(llm_mod, "_build_excluded_string", return_value=""),
        patch.object(llm_mod, "_chat", return_value=_batch_response(tags=["#ant"])) as chat,
    ):
        batch_generate_hashtags([("a.mp4", "topic")], "youtube", lang_override="Spanish")

    prompt = chat.call_args.args[0]
    assert "Spanish" in prompt
    assert "language of each video" not in prompt


def test_batch_prompt_without_lang_asks_for_detection():
    from mpt_autopilot.enricher import llm as llm_mod

    with (
        patch.object(llm_mod, "settings", _llm_settings()),
        patch.object(llm_mod, "_build_excluded_string", return_value=""),
        patch.object(llm_mod, "_chat", return_value=_batch_response(tags=["#ant"])) as chat,
    ):
        batch_generate_hashtags([("a.mp4", "topic")], "youtube")

    assert "language of each video" in chat.call_args.args[0]


def test_forced_language_wins_in_batch_sidecar():
    """_write_batch_result writes the --lang value, not the model's guess."""
    from mpt_autopilot.enricher import run as run_mod

    mp4 = Path("video.mp4")
    result = {"filename": "video.mp4", "language": "Italian", "tags": ["#a"]}
    block = build_hashtags_block(["#a"], "Spanish", "m", "filename")

    with (
        patch.object(run_mod, "resolve_meta") as mock_meta,
        patch.object(run_mod, "write_hashtags") as mock_write,
        patch.object(run_mod, "build_hashtags_block", return_value=block),
        patch.object(run_mod, "_get_log"),
        patch.object(run_mod, "settings", _llm_settings()),
    ):
        mock_meta.return_value = MagicMock(json_path=Path("video.json"), source="filename")
        written = run_mod._write_batch_result(
            mp4, result, platform_override=None, force=False, lang_override="Spanish"
        )

    assert written == "ok"
    # build_hashtags_block was called with the forced language
    args = mock_write.call_args  # (json_path, block) — check the block's language
    assert args.args[1]["detected_language"] == "Spanish"


# ── M-37: config coercion ─────────────────────────────────────────────────────


def _settings_from(cfg: dict):
    """Build a real Settings against a mocked shared loader (same pattern as
    test_batch_hashtags.py's validation tests)."""
    from mpt_autopilot.enricher import settings as settings_mod

    with patch.object(settings_mod.shared_config, "load") as mock_load:
        mock_shared = MagicMock()
        mock_shared.section.return_value = cfg
        mock_shared.path.return_value = Path("/dev/null")
        mock_shared.require.return_value = ""
        mock_shared.path_value.side_effect = lambda section, key, default=None: (
            Path("/tmp") if key == "log_dir" else None
        )
        mock_shared.telegram_prefix.return_value = ""
        mock_load.return_value = mock_shared

        with patch.dict("os.environ", {"LLM_API_KEY": "test-key"}):
            return settings_mod.Settings()


def test_platform_must_be_a_string():
    with pytest.raises(ValueError, match="enricher.platform must be a string"):
        _settings_from({"platform": 123})


def test_banned_tags_rejects_non_string_entries():
    with pytest.raises(ValueError, match="must be strings"):
        _settings_from({"banned_tags": ["#ok", 5]})


def test_banned_tags_wraps_a_bare_string():
    s = _settings_from({"banned_tags": "#spam"})
    assert s.banned_tags == {"#spam"}


def test_always_include_scalar_is_wrapped_not_counted_as_chars():
    """`always_include: #shorts` used to become the string '#shorts', and
    len('#shorts') == 7 silently inflated the tag budget math."""
    s = _settings_from({"always_include": "#shorts"})
    assert s.always_include == ["#shorts"]
    assert len(s.always_include) == 1


def test_supports_temperature_stringly_false_is_false():
    s = _settings_from({"supports_temperature": "false"})
    assert s.supports_temperature is False


def test_supports_temperature_stringly_true_is_true():
    s = _settings_from({"supports_temperature": "true"})
    assert s.supports_temperature is True


def test_supports_temperature_rejects_non_booleans():
    with pytest.raises(ValueError, match="supports_temperature"):
        _settings_from({"supports_temperature": 1})


# ── M-38: _chat without choices[] ─────────────────────────────────────────────


def test_chat_without_choices_raises_named_error():
    from mpt_autopilot.enricher import llm as llm_mod

    response = MagicMock()
    response.json.return_value = {"error": {"message": "boom"}}

    with (
        patch.object(llm_mod, "settings", _llm_settings()),
        patch.object(llm_mod, "_get_log"),
        patch.object(llm_mod, "retrying_request", return_value=response),
    ):
        with pytest.raises(ValueError, match=r"choices\[0\]"):
            llm_mod._chat("prompt")


def test_chat_with_empty_choices_list_raises_named_error():
    from mpt_autopilot.enricher import llm as llm_mod

    response = MagicMock()
    response.json.return_value = {"choices": []}

    with (
        patch.object(llm_mod, "settings", _llm_settings()),
        patch.object(llm_mod, "_get_log"),
        patch.object(llm_mod, "retrying_request", return_value=response),
    ):
        with pytest.raises(ValueError, match=r"choices\[0\]"):
            llm_mod._chat("prompt")


# ── M-36 / L: writer backups ──────────────────────────────────────────────────


def _block() -> dict:
    return build_hashtags_block(["#ant"], "English", "test-model", "filename")


def test_write_hashtags_backs_up_array_sidecar(tmp_path: Path):
    """A valid-JSON top-level array used to crash write_hashtags with a
    TypeError (M-36); now it is backed up and replaced like any unreadable
    file."""
    sidecar = tmp_path / "v.json"
    sidecar.write_text("[1, 2, 3]", encoding="utf-8")

    write_hashtags(sidecar, _block())

    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["hashtags"]["tags_list"] == ["#ant"]
    assert json.loads((tmp_path / "v.json.bak").read_text(encoding="utf-8")) == [1, 2, 3]


def test_second_bad_run_keeps_the_first_backup(tmp_path: Path):
    """The .bak used to be unlinked first, so a second bad run destroyed the
    only surviving copy of the original."""
    sidecar = tmp_path / "v.json"
    sidecar.write_text("not json at all", encoding="utf-8")

    write_hashtags(sidecar, _block())
    first_backup = tmp_path / "v.json.bak"
    assert first_backup.read_text(encoding="utf-8") == "not json at all"

    # Break it again — the first backup must survive untouched.
    sidecar.write_text("[1]", encoding="utf-8")
    write_hashtags(sidecar, _block())

    assert first_backup.read_text(encoding="utf-8") == "not json at all"
    assert (tmp_path / "v.json.bak.1").read_text(encoding="utf-8") == "[1]"


# ── L: validate_and_filter preserves first-occurrence casing ─────────────────


def test_validate_and_filter_keeps_first_occurrence_casing():
    assert validate_and_filter(["#History", "#history"]) == ["#History"]


# ── A-4/R2: title + description ──────────────────────────────────────────────


def test_resolve_meta_reads_the_script_from_script_json(tmp_path: Path):
    """A-4's preparatory step: the hook of a good title lives in the script's
    first line, which resolve_meta used to throw away (only video_subject was
    read, capped at 300 chars)."""
    from mpt_autopilot.enricher.reader import resolve_meta

    mp4 = tmp_path / "v.mp4"
    (tmp_path / "v.json").write_text(
        json.dumps(
            {
                "script": "Hook line.\n\nSecond paragraph.",
                "params": {"video_subject": "Topic here", "video_language": "en"},
            }
        ),
        encoding="utf-8",
    )
    meta = resolve_meta(mp4)
    assert meta.script.startswith("Hook line.")
    assert meta.topic == "Topic here"


def test_generate_title_description_caps_the_title(tmp_path: Path):
    from mpt_autopilot.enricher import llm as llm_mod

    long_title = "T" * 150
    with (
        patch.object(llm_mod, "settings", _llm_settings()),
        patch.object(
            llm_mod,
            "_chat",
            return_value='```json\n{"title": "' + long_title + '", "description": "d"}\n```',
        ),
    ):
        result = llm_mod.generate_title_description("topic", "script", "English", "youtube")

    assert len(result["title"]) == 100
    assert result["description"] == "d"


def test_generate_title_description_rejects_an_empty_title(tmp_path: Path):
    from mpt_autopilot.enricher import llm as llm_mod

    with (
        patch.object(llm_mod, "settings", _llm_settings()),
        patch.object(llm_mod, "_chat", return_value='{"title": "", "description": "d"}'),
    ):
        with pytest.raises(ValueError, match="empty 'title'"):
            llm_mod.generate_title_description("topic", "", "English", "youtube")


def test_write_hashtags_persists_extra_top_level_fields(tmp_path: Path):
    """title/description are written at the sidecar's TOP level — exactly
    where uploader/metadata.py reads them."""
    sidecar = tmp_path / "v.json"
    block = build_hashtags_block(["#ant"], "English", "test-model", "filename")
    write_hashtags(sidecar, block, extra_fields={"title": "The Title", "description": "The D"})
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["title"] == "The Title"
    assert data["description"] == "The D"
    assert data["hashtags"]["tags_list"] == ["#ant"]
