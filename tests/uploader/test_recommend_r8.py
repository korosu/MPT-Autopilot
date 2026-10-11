"""Tests for R8/R9 in the recommend plan: the ledger's video_id column and
the validate.py prompt-health checks."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from mpt_autopilot.uploader.ledger import (
    mark_moved,
    mark_started,
    mark_uploaded,
    open_ledger,
)
from mpt_autopilot.uploader.run import _video_id_from_output

# ── R8: video_id column + legacy-ledger migration ────────────────────────────


def test_mark_uploaded_persists_the_video_id(tmp_path: Path):
    conn = open_ledger(tmp_path / "ledger.sqlite")
    try:
        mark_started(conn, "en", "hash-1", "a.mp4")
        mark_uploaded(conn, "en", "hash-1", "dQw4w9WgXcQ")
        row = conn.execute(
            "SELECT status, video_id FROM uploads WHERE account='en' AND content_hash='hash-1'"
        ).fetchone()
        assert row == ("uploaded", "dQw4w9WgXcQ")
    finally:
        conn.close()


def test_legacy_ledger_without_video_id_column_is_migrated(tmp_path: Path):
    """R8: CREATE TABLE IF NOT EXISTS never teaches an existing database new
    columns — a pre-video_id ledger must be migrated on open, or every
    UPDATE that mentions the column fails."""
    legacy = tmp_path / "ledger.sqlite"
    conn = sqlite3.connect(legacy)
    conn.execute(
        "CREATE TABLE uploads ("
        "account TEXT NOT NULL, content_hash TEXT NOT NULL, video_name TEXT, "
        "status TEXT NOT NULL, created_at TEXT NOT NULL, "
        "PRIMARY KEY (account, content_hash))"
    )
    conn.execute(
        "INSERT INTO uploads VALUES ('en', 'old-hash', 'old.mp4', 'started', '2026-01-01')"
    )
    conn.commit()
    conn.close()

    conn = open_ledger(legacy)
    try:
        mark_uploaded(conn, "en", "old-hash", "abc12345678")
        row = conn.execute(
            "SELECT status, video_id FROM uploads WHERE content_hash='old-hash'"
        ).fetchone()
        assert row == ("uploaded", "abc12345678")
    finally:
        conn.close()


def test_mark_uploaded_without_video_id_leaves_previous_value(tmp_path: Path):
    conn = open_ledger(tmp_path / "ledger.sqlite")
    try:
        mark_started(conn, "en", "hash-1", "a.mp4")
        mark_uploaded(conn, "en", "hash-1", "abc12345678")
        # A later run without a parsed id must not erase the stored mapping.
        mark_uploaded(conn, "en", "hash-1")
        mark_moved(conn, "en", "hash-1")
        row = conn.execute(
            "SELECT status, video_id FROM uploads WHERE content_hash='hash-1'"
        ).fetchone()
        assert row == ("moved", "abc12345678")
    finally:
        conn.close()


# ── R8: parsing the id from youtubeuploader's output ──────────────────────────


def test_video_id_from_short_url():
    assert _video_id_from_output("Video uploaded: https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_video_id_from_watch_url():
    output = "done https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=0s"
    assert _video_id_from_output(output) == "dQw4w9WgXcQ"


def test_video_id_from_plain_output_is_none():
    assert _video_id_from_output("all good, no url printed") is None


# ── R9.2/R9.3: validate.py prompt-health checks ───────────────────────────────


def _config(tmp_path: Path, enricher_body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        f'langs:\n  en:\n    label: English\n    file_suffix: ""\nenricher:\n{enricher_body}',
        encoding="utf-8",
    )
    return path


def test_validate_flags_unknown_prompt_placeholder(tmp_path: Path):
    from mpt_autopilot import validate

    cfg = validate.shared_config.load(
        _config(
            tmp_path,
            "  max_tag_length: 20\n  prompt_generate: |\n    Tags for {video_subject}"
            " and {tag_count}.\n",
        )
    )
    results = validate._check_prompt_health(cfg)
    assert any("{tag_count}" not in r.detail and "tag_count" in r.detail for r in results), (
        "the unknown placeholder should be named"
    )


def test_validate_flags_overlong_example_hashtags(tmp_path: Path):
    from mpt_autopilot import validate

    cfg = validate.shared_config.load(
        _config(
            tmp_path,
            "  max_tag_length: 20\n"
            "  prompt_generate: |\n"
            "    Good: #octopusfacts. Bad example: #curiosidadesdeluniverso\n",
        )
    )
    results = validate._check_prompt_health(cfg)
    assert any("#curiosidadesdeluniverso" in r.detail for r in results)


def test_validate_is_silent_on_a_healthy_prompt(tmp_path: Path):
    from mpt_autopilot import validate

    cfg = validate.shared_config.load(
        _config(
            tmp_path,
            "  max_tag_length: 20\n"
            "  prompt_generate: |\n"
            "    Tags for {video_subject} on {platform}, {min_tags}-{max_tags} tags,\n"
            "    max {max_tag_length} chars. Example: #octopusfacts\n"
            '    JSON: {{"results": []}}\n',
        )
    )
    assert validate._check_prompt_health(cfg) == []


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
