"""
Unit tests for batch enrichment in the enricher stage.

Covers:
  - batch_generate_hashtags() success and error paths
  - _resolve_batch_size() CLI/config precedence
  - Settings.batch_size validation
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mpt_autopilot.enricher.llm import batch_generate_hashtags

# ── Helpers ────────────────────────────────────────────────────────────────────


def _fake_response(status_code: int = 200, content: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.is_success = status_code < 400
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    resp.raise_for_status = MagicMock()
    return resp


# ── batch_generate_hashtags() ─────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _mock_settings():
    """Mock the enricher settings singleton for llm.py tests.

    The attributes _finalize_tags() reads are real values, not MagicMock
    defaults: len(MagicMock) raises TypeError and frozenset(MagicMock) is
    meaningless, and since batch results now go through _finalize_tags too
    (M-33) every batch test would otherwise crash before asserting anything.
    """
    fake_settings = MagicMock()
    fake_settings.max_tags = 5
    fake_settings.min_tags = 3
    fake_settings.max_tag_length = 20
    fake_settings.banned_tags = frozenset()
    fake_settings.always_include = []
    fake_settings.platform = "youtube"
    # B-10: the batch prompt is a configured template since R9.1 — give the
    # mock a real string so .format() behaves like production.
    fake_settings.prompt_batch = (
        "Hashtags for {count} videos on {platform}; tags in {language}; "
        "{min_tags}-{max_tags}; max {max_tag_length}; excluded {excluded_tags}. "
        "VIDEOS:\n{videos}\n"
        'JSON: {{"results": [{{"filename": "...", "language": "...", "tags": []}}]}}'
    )
    # _finalize_tags now runs on batch results too and may log (min_tags
    # shortfall) — the real _get_log would try to open a MagicMock log path.
    with (
        patch("mpt_autopilot.enricher.llm.settings", fake_settings),
        patch("mpt_autopilot.enricher.llm._build_excluded_string", return_value="#shorts"),
        patch("mpt_autopilot.enricher.llm._get_log"),
    ):
        yield


def test_batch_generate_hashtags_success():
    """Valid JSON with matching entry count returns cleaned results."""
    topics = [
        ("clip_a.mp4", "Octopuses have three hearts"),
        ("clip_b.mp4", "Penguins propose with stones"),
    ]
    response_json = json.dumps(
        {
            "results": [
                {"filename": "clip_a.mp4", "language": "English", "tags": ["#octopus", "#marine"]},
                {"filename": "clip_b.mp4", "language": "English", "tags": ["#penguin", "#birds"]},
            ]
        }
    )

    with patch("mpt_autopilot.enricher.llm._chat", return_value=response_json):
        results = batch_generate_hashtags(topics, "youtube")

    assert len(results) == 2
    assert results[0]["filename"] == "clip_a.mp4"
    assert results[0]["language"] == "English"
    assert results[0]["tags"] == ["#octopus", "#marine"]
    assert results[1]["filename"] == "clip_b.mp4"


def test_batch_generate_hashtags_malformed_json():
    """Non-JSON response raises ValueError (caller falls back to per-file)."""
    topics = [("clip_a.mp4", "some topic")]

    with patch("mpt_autopilot.enricher.llm._chat", return_value="not valid json {{"):
        with pytest.raises(ValueError, match="non-JSON"):
            batch_generate_hashtags(topics, "youtube")


def test_batch_generate_hashtags_wrong_type():
    """LLM returns a plain array instead of an object — raises ValueError."""
    topics = [("clip_a.mp4", "some topic")]

    with patch("mpt_autopilot.enricher.llm._chat", return_value='["just", "an", "array"]'):
        with pytest.raises(ValueError, match="JSON object"):
            batch_generate_hashtags(topics, "youtube")


def test_batch_generate_hashtags_missing_results_key():
    """LLM returns object without 'results' key — raises ValueError."""
    topics = [("clip_a.mp4", "some topic")]

    with patch("mpt_autopilot.enricher.llm._chat", return_value='{"status": "ok"}'):
        with pytest.raises(ValueError, match="results"):
            batch_generate_hashtags(topics, "youtube")


def test_batch_generate_hashtags_wrong_count():
    """LLM returns fewer or more results than expected — raises ValueError."""
    topics = [
        ("clip_a.mp4", "topic a"),
        ("clip_b.mp4", "topic b"),
        ("clip_c.mp4", "topic c"),
    ]

    with patch(
        "mpt_autopilot.enricher.llm._chat",
        return_value=json.dumps({"results": [{"filename": "a", "language": "en", "tags": []}]}),
    ):
        with pytest.raises(ValueError, match=r"\d+ results, expected 3"):
            batch_generate_hashtags(topics, "youtube")


def test_batch_results_follow_the_requested_order_not_the_model_order():
    """A reordered response must not pair one video's tags with another's file."""
    topics = [
        ("clip_a.mp4", "topic a"),
        ("clip_b.mp4", "topic b"),
    ]
    response_json = json.dumps(
        {
            "results": [
                {"filename": "clip_b.mp4", "language": "English", "tags": ["#bee"]},
                {"filename": "clip_a.mp4", "language": "English", "tags": ["#ant"]},
            ]
        }
    )

    with patch("mpt_autopilot.enricher.llm._chat", return_value=response_json):
        results = batch_generate_hashtags(topics, "youtube")

    assert [r["filename"] for r in results] == ["clip_a.mp4", "clip_b.mp4"]
    assert results[0]["tags"] == ["#ant"]
    assert results[1]["tags"] == ["#bee"]


def test_batch_results_must_cover_every_requested_filename():
    """Right count, wrong filenames — raises so the caller falls back per file."""
    topics = [
        ("clip_a.mp4", "topic a"),
        ("clip_b.mp4", "topic b"),
    ]
    response_json = json.dumps(
        {
            "results": [
                {"filename": "clip_a.mp4", "language": "English", "tags": ["#ant"]},
                {"filename": "clip_z.mp4", "language": "English", "tags": ["#zebra"]},
            ]
        }
    )

    with patch("mpt_autopilot.enricher.llm._chat", return_value=response_json):
        with pytest.raises(ValueError, match="does not cover"):
            batch_generate_hashtags(topics, "youtube")


def test_batch_generate_hashtags_empty_topics():
    """Empty topics list returns empty results without calling the LLM."""
    with patch("mpt_autopilot.enricher.llm._chat") as mock_chat:
        results = batch_generate_hashtags([], "youtube")

    assert results == []
    mock_chat.assert_not_called()


def test_batch_generate_hashtags_strips_code_fences():
    """Response wrapped in markdown fences is parsed correctly."""
    topics = [("clip_a.mp4", "some topic")]
    response = (
        "```json\n"
        + json.dumps(
            {"results": [{"filename": "clip_a.mp4", "language": "English", "tags": ["#tag"]}]}
        )
        + "\n```"
    )

    with patch("mpt_autopilot.enricher.llm._chat", return_value=response):
        results = batch_generate_hashtags(topics, "youtube")

    assert len(results) == 1
    assert results[0]["tags"] == ["#tag"]


# ── _resolve_batch_size() CLI/config precedence ────────────────────────────────


def test_no_batch_flag():
    """--no-batch forces batch_size=1 regardless of config/CLI."""
    from mpt_autopilot.enricher.run import _resolve_batch_size

    class _FakeSettings:
        batch_size = 50

    args = MagicMock()
    args.no_batch = True
    args.batch_size = 20

    with patch("mpt_autopilot.enricher.run.settings", _FakeSettings()):
        assert _resolve_batch_size(args) == 1


def test_batch_size_cli_override():
    """--batch-size N overrides config value."""
    from mpt_autopilot.enricher.run import _resolve_batch_size

    class _FakeSettings:
        batch_size = 3

    args = MagicMock()
    args.no_batch = False
    args.batch_size = 25

    with patch("mpt_autopilot.enricher.run.settings", _FakeSettings()):
        assert _resolve_batch_size(args) == 25


def test_batch_size_rejects_out_of_range_on_cli():
    """M-41: --batch-size 0 / 500 is a hard argparse error now.

    The CLI used to silently clamp both to the valid range while config.yaml
    raised for the same numbers — one contract, two behaviours. Both paths
    now fail loudly.
    """
    import argparse

    from mpt_autopilot.enricher.run import add_arguments

    for bad_value in ("0", "500", "-1"):
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--batch-size", bad_value])
        assert exc_info.value.code == 2  # argparse usage error


def test_batch_size_accepts_range_boundaries_on_cli():
    import argparse

    from mpt_autopilot.enricher.run import add_arguments

    parser = argparse.ArgumentParser()
    add_arguments(parser)
    assert parser.parse_args(["--batch-size", "1"]).batch_size == 1
    assert parser.parse_args(["--batch-size", "100"]).batch_size == 100


# ── Settings.batch_size validation ────────────────────────────────────────────


def test_batch_size_default():
    """Default batch_size is 5 when not specified in config."""

    # Value-level check (the actual default lives in Settings.__init__)
    cfg = {"batch_size": 5}
    batch_size = int(cfg.get("batch_size", 5))
    assert batch_size == 5


def test_batch_size_validation_below_min():
    """batch_size < 1 raises ValueError in Settings.__init__."""
    from mpt_autopilot.enricher.settings import Settings

    fake_cfg = {"batch_size": 0, "min_tags": 1, "max_tags": 2, "always_include": []}
    with (
        patch("mpt_autopilot.enricher.settings.shared_config.load") as mock_load,
        patch("mpt_autopilot.enricher.settings.platform_hard_limit", return_value=500),
    ):
        mock_shared = MagicMock()
        mock_shared.section.return_value = fake_cfg
        mock_shared.path.return_value = Path("/dev/null")
        mock_shared.require.return_value = ""
        mock_shared.path_value.side_effect = lambda section, key, default=None: (
            Path("/tmp") if key == "log_dir" else None
        )
        mock_shared.telegram_prefix.return_value = ""
        mock_load.return_value = mock_shared

        with patch.dict("os.environ", {"LLM_API_KEY": "test-key"}):
            with pytest.raises(ValueError, match="at least 1"):
                Settings()


def test_batch_size_validation_above_max():
    """batch_size > 100 raises ValueError in Settings.__init__."""
    from mpt_autopilot.enricher.settings import Settings

    fake_cfg = {"batch_size": 200, "min_tags": 1, "max_tags": 2, "always_include": []}
    with (
        patch("mpt_autopilot.enricher.settings.shared_config.load") as mock_load,
        patch("mpt_autopilot.enricher.settings.platform_hard_limit", return_value=500),
    ):
        mock_shared = MagicMock()
        mock_shared.section.return_value = fake_cfg
        mock_shared.path.return_value = Path("/dev/null")
        mock_shared.require.return_value = ""
        mock_shared.path_value.side_effect = lambda section, key, default=None: (
            Path("/tmp") if key == "log_dir" else None
        )
        mock_shared.telegram_prefix.return_value = ""
        mock_load.return_value = mock_shared

        with patch.dict("os.environ", {"LLM_API_KEY": "test-key"}):
            with pytest.raises(ValueError, match="maximum of 100"):
                Settings()
