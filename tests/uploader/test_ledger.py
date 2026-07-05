from __future__ import annotations

from pathlib import Path

import pytest

from yt_uploader.engine.ledger import (
    mark_started,
    mark_uploaded,
    open_ledger,
    sha256_file,
)
from yt_uploader.engine.settings import Account, Defaults, Settings
from yt_uploader.upload import EXIT_OK, run


def _account(tmp_path: Path, n_videos: int) -> Account:
    videos_dir = tmp_path / "videos"
    videos_dir.mkdir()
    for i in range(n_videos):
        # Distinct content per video to avoid SHA-256 hash collisions in ledger dedup
        (videos_dir / f"video_{i}.mp4").write_bytes(f"video_{i} content".encode())
    secrets = tmp_path / "secrets.json"
    secrets.write_text("{}")
    return Account(
        name="en",
        videos_dir=videos_dir,
        client_secrets=secrets,
        token_file=tmp_path / "token",
    )


def _settings(tmp_path: Path) -> Settings:
    binary = tmp_path / "youtubeuploader"
    binary.write_text("#!/bin/bash\n")
    binary.chmod(0o755)
    return Settings(
        uploader_binary=binary,
        meta_dir=tmp_path / "meta",
        sleep_between_uploads=0,
        uploaded_dir_name="old_videos",
        defaults=Defaults(),
        notify_enabled=False,
        telegram_token="",
        telegram_chat_id="",
        discord_webhook_url="",
        ledger_path=tmp_path / "ledger.sqlite",
        accounts={},
    )


def test_ledger_skips_video_marked_uploaded(tmp_path, monkeypatch):
    account = _account(tmp_path, 3)
    settings = _settings(tmp_path)

    video_to_skip = account.videos_dir / "video_1.mp4"
    content_hash = sha256_file(video_to_skip)

    conn = open_ledger(settings.ledger_path)
    # mark_started inserts the row; mark_uploaded updates it to 'uploaded'
    mark_started(conn, account.name, content_hash, "video_1.mp4")
    mark_uploaded(conn, account.name, content_hash)
    conn.close()

    called = []
    monkeypatch.setattr("yt_uploader.upload.upload_video", lambda *a, **k: called.append(1) or "ok")

    exit_code = run(settings, account, dry_run=False, limit=None)

    assert exit_code == EXIT_OK
    assert len(called) == 2  # video_0 and video_2 were uploaded; video_1 was skipped
    # skipped video was not moved, still at source
    assert (account.videos_dir / "video_1.mp4").exists()
    assert len(list((account.videos_dir / "old_videos").glob("*.mp4"))) == 2


def test_ledger_retries_started_row(tmp_path, monkeypatch):
    account = _account(tmp_path, 1)
    settings = _settings(tmp_path)

    video = account.videos_dir / "video_0.mp4"
    content_hash = sha256_file(video)

    conn = open_ledger(settings.ledger_path)
    mark_started(conn, account.name, content_hash, "video_0.mp4")
    conn.close()

    called = []
    monkeypatch.setattr("yt_uploader.upload.upload_video", lambda *a, **k: called.append(1) or "ok")

    exit_code = run(settings, account, dry_run=False, limit=None)

    assert exit_code == EXIT_OK
    assert len(called) == 1  # video was retried (row was 'started', not skipped)


def test_ledger_marks_moved_after_success(tmp_path, monkeypatch):
    account = _account(tmp_path, 2)
    settings = _settings(tmp_path)
    monkeypatch.setattr("yt_uploader.upload.upload_video", lambda *a, **k: "ok")

    exit_code = run(settings, account, dry_run=False, limit=None)

    assert exit_code == EXIT_OK

    uploaded_dir = account.videos_dir / "old_videos"
    assert len(list(uploaded_dir.glob("*.mp4"))) == 2

    conn = open_ledger(settings.ledger_path)
    for i in range(2):
        video = account.videos_dir / f"video_{i}.mp4"
        # video was moved, so it no longer exists at source
        assert not video.exists()
    # ledger shows both as 'moved'
    cur = conn.execute(
        "SELECT status FROM uploads WHERE account = ?", (account.name,)
    )
    statuses = [row[0] for row in cur.fetchall()]
    assert all(s == "moved" for s in statuses)
    conn.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
