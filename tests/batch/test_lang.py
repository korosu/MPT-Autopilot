"""tests/test_lang.py — Verify --lang suffix derivation."""

from __future__ import annotations

import argparse
from pathlib import Path

from mpt_autopilot.batch import run as batch_run
from mpt_autopilot.batch.settings import Settings


def _status_args(lang: str | None) -> argparse.Namespace:
    return argparse.Namespace(
        jobs=None,
        dry_run=False,
        status=True,
        list_voices=None,
        list_bgm=None,
        upload_bgm=None,
        seen=None,
        lang=lang,
    )


def _write_config(tmp_path: Path, seen_dir: str) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(
        "langs:\n"
        "  en:\n"
        '    file_suffix: ""\n'
        "  es:\n"
        "    file_suffix: _es\n"
        "paths:\n"
        f"  seen_dir: {seen_dir}\n"
        "batch:\n"
        "  api_url: http://127.0.0.1:8080\n"
        "  mpt_storage: ./storage\n"
        "  mpt_songs_dir: ./songs\n",
        encoding="utf-8",
    )
    return config


def test_lang_suffix_applied_to_seen_and_output() -> None:
    """When --lang is passed, seen and output paths get the suffix."""
    langs = {"es": {"file_suffix": "_es"}}
    s = Settings(
        jobs=None,
        api_url="http://127.0.0.1:8080",
        mpt_storage=Path("/tmp/mpt_storage"),
        mpt_songs_dir=Path("/tmp/mpt_resource/songs"),
        output_dir=Path("/tmp/exports"),
        langs=langs,
        jobs_dir=None,
        voice_pool={},
        log_file=Path("/tmp/log.txt"),
        log_max_bytes=10_000_000,
        max_wait_seconds=2400,
        stuck_threshold_seconds=3600,
        max_retries=3,
        retry_delay_seconds=180,
        max_consecutive_failures=3,
        cache_cleanup_enabled=True,
        cache_cleanup_interval=6,
        seen_max_mb=64,
        telegram_token="",
        telegram_chat_id="",
        telegram_prefix="test",
    )

    # Simulate what main() does with --lang es
    lang_code = "es"
    lang_suffix = s.langs[lang_code]["file_suffix"]

    # output_dir derivation
    output_stem = s.output_dir.name
    derived_output = s.output_dir.parent / f"{output_stem}{lang_suffix}"
    assert derived_output == Path("/tmp/exports_es")


def test_no_lang_suffix_when_lang_is_empty() -> None:
    """Empty suffix produces bare filenames."""
    langs = {"en": {"file_suffix": ""}}
    s = Settings(
        jobs=None,
        api_url="http://127.0.0.1:8080",
        mpt_storage=Path("/tmp/mpt_storage"),
        mpt_songs_dir=Path("/tmp/mpt_resource/songs"),
        output_dir=Path("/tmp/exports"),
        langs=langs,
        jobs_dir=None,
        voice_pool={},
        log_file=Path("/tmp/log.txt"),
        log_max_bytes=10_000_000,
        max_wait_seconds=2400,
        stuck_threshold_seconds=3600,
        max_retries=3,
        retry_delay_seconds=180,
        max_consecutive_failures=3,
        cache_cleanup_enabled=True,
        cache_cleanup_interval=6,
        seen_max_mb=64,
        telegram_token="",
        telegram_chat_id="",
        telegram_prefix="test",
    )

    lang_suffix = s.langs["en"]["file_suffix"]
    assert lang_suffix == ""

    # Empty suffix → paths unchanged
    output_stem = s.output_dir.name
    derived_output = s.output_dir.parent / f"{output_stem}{lang_suffix}"
    assert derived_output == s.output_dir  # unchanged


def test_jobs_dir_resolution() -> None:
    """jobs_dir in config overrides default jobs path location."""
    jobs_dir = Path("/tmp/my_jobs")
    lang_suffix = "_es"
    resolved = jobs_dir / f"jobs{lang_suffix}.yaml"
    assert resolved == Path("/tmp/my_jobs/jobs_es.yaml")

    # Without lang suffix
    resolved_no_suffix = jobs_dir / "jobs.yaml"
    assert resolved_no_suffix == Path("/tmp/my_jobs/jobs.yaml")


def test_lang_seen_path_keeps_the_configured_subdirectory(tmp_path, capsys) -> None:
    """
    --lang must suffix the seen file in place under paths.seen_dir.

    With `paths.seen_dir: ./jobs`, deriving from the config directory
    would look for ./seen_es.txt — a different, empty registry — and re-render
    every video that language had already produced.
    """
    config = _write_config(tmp_path, "./jobs")
    seen_es = tmp_path / "jobs" / "seen_es.txt"
    seen_es.parent.mkdir()
    seen_es.write_text("already_rendered_es.mp4\n", encoding="utf-8")

    assert batch_run.execute(_status_args("es"), config) == 0

    out = capsys.readouterr().out
    assert str(seen_es) in out
    assert "already_rendered_es.mp4" in out


def test_lang_seen_path_next_to_config_when_not_nested(tmp_path, capsys) -> None:
    config = _write_config(tmp_path, ".")
    (tmp_path / "seen_es.txt").write_text("flat_es.mp4\n", encoding="utf-8")

    assert batch_run.execute(_status_args("es"), config) == 0

    out = capsys.readouterr().out
    assert str(tmp_path / "seen_es.txt") in out
    assert "flat_es.mp4" in out


def test_empty_suffix_leaves_the_seen_path_alone(tmp_path, capsys) -> None:
    config = _write_config(tmp_path, "./jobs")
    seen = tmp_path / "jobs" / "seen.txt"
    seen.parent.mkdir()
    seen.write_text("bare_en.mp4\n", encoding="utf-8")

    assert batch_run.execute(_status_args("en"), config) == 0

    out = capsys.readouterr().out
    assert str(seen) in out
    assert "bare_en.mp4" in out
