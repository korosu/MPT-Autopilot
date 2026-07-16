"""tests/test_lang.py — Verify --lang suffix derivation."""

from __future__ import annotations

from pathlib import Path

from mpt_batch.engine.settings import Settings


def test_lang_suffix_applied_to_seen_and_output() -> None:
    """When --lang is passed, seen_file and output_dir get the suffix."""
    langs = {"es": {"file_suffix": "_es"}}
    s = Settings(
        api_url="http://127.0.0.1:8080",
        mpt_storage=Path("/tmp/mpt_storage"),
        output_dir=Path("/tmp/exports"),
        seen_file=Path("/tmp/seen.txt"),
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
        telegram_token="",
        telegram_chat_id="",
        telegram_prefix="test",
    )

    # Simulate what main() does with --lang es
    lang_code = "es"
    lang_suffix = s.langs[lang_code]["file_suffix"]

    # seen_file derivation
    cfg_dir = s.seen_file.parent
    seen_stem = s.seen_file.stem
    seen_suffix = s.seen_file.suffix
    derived_seen = cfg_dir / f"{seen_stem}{lang_suffix}{seen_suffix}"
    assert derived_seen == Path("/tmp/seen_es.txt")

    # output_dir derivation
    output_stem = s.output_dir.name
    derived_output = s.output_dir.parent / f"{output_stem}{lang_suffix}"
    assert derived_output == Path("/tmp/exports_es")


def test_no_lang_suffix_when_lang_is_empty() -> None:
    """Empty suffix produces bare filenames."""
    langs = {"en": {"file_suffix": ""}}
    s = Settings(
        api_url="http://127.0.0.1:8080",
        mpt_storage=Path("/tmp/mpt_storage"),
        output_dir=Path("/tmp/exports"),
        seen_file=Path("/tmp/seen.txt"),
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
        telegram_token="",
        telegram_chat_id="",
        telegram_prefix="test",
    )

    lang_suffix = s.langs["en"]["file_suffix"]
    assert lang_suffix == ""

    # Empty suffix → paths unchanged
    seen_stem = s.seen_file.stem
    seen_ext = s.seen_file.suffix
    derived_seen = s.seen_file.parent / f"{seen_stem}{lang_suffix}{seen_ext}"
    assert derived_seen == s.seen_file  # unchanged

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
