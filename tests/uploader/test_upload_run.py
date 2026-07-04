from __future__ import annotations

from pathlib import Path

import pytest

from yt_uploader.engine.settings import Account, Defaults, Settings
from yt_uploader.engine.uploader import UploadFailed, UploadLimitExceeded
from yt_uploader.upload import EXIT_FAILURES, EXIT_OK, EXIT_QUOTA_STOP, run


def _account(tmp_path: Path, n_videos: int, *, daily_limit: int | None = None) -> Account:
    videos_dir = tmp_path / "videos"
    videos_dir.mkdir()
    for i in range(n_videos):
        (videos_dir / f"video_{i}.mp4").write_bytes(b"fake")

    secrets = tmp_path / "secrets.json"
    secrets.write_text("{}")

    return Account(
        name="en",
        videos_dir=videos_dir,
        client_secrets=secrets,
        token_file=tmp_path / "token",
        daily_upload_limit=daily_limit,
    )


def _settings(tmp_path: Path, *, sleep: int = 0) -> Settings:
    binary = tmp_path / "youtubeuploader"
    binary.write_text("#!/bin/bash\n")
    binary.chmod(0o755)
    return Settings(
        uploader_binary=binary,
        meta_dir=tmp_path / "meta",
        sleep_between_uploads=sleep,
        uploaded_dir_name="old_videos",
        defaults=Defaults(),
        notify_enabled=False,
        telegram_token="",
        telegram_chat_id="",
        accounts={},
    )


def test_run_all_succeed_returns_ok(tmp_path, monkeypatch):
    account = _account(tmp_path, 3)
    settings = _settings(tmp_path)
    monkeypatch.setattr("yt_uploader.upload.upload_video", lambda *a, **k: "ok")

    exit_code = run(settings, account, dry_run=False, limit=None)

    assert exit_code == EXIT_OK
    uploaded = list((account.videos_dir / "old_videos").glob("*.mp4"))
    assert len(uploaded) == 3


def test_run_quota_stop_returns_distinct_code(tmp_path, monkeypatch):
    account = _account(tmp_path, 3)
    settings = _settings(tmp_path)

    def fake_upload(*a, **k):
        raise UploadLimitExceeded("uploadLimitExceeded")

    monkeypatch.setattr("yt_uploader.upload.upload_video", fake_upload)

    exit_code = run(settings, account, dry_run=False, limit=None)

    assert exit_code == EXIT_QUOTA_STOP
    assert exit_code != EXIT_OK  # this is the bug we're guarding against


def test_run_failure_returns_1(tmp_path, monkeypatch):
    account = _account(tmp_path, 2)
    settings = _settings(tmp_path)

    def fake_upload(*a, **k):
        raise UploadFailed("boom")

    monkeypatch.setattr("yt_uploader.upload.upload_video", fake_upload)

    exit_code = run(settings, account, dry_run=False, limit=None)

    assert exit_code == EXIT_FAILURES


def test_run_skips_sleep_after_last_video(tmp_path, monkeypatch):
    account = _account(tmp_path, 3)
    settings = _settings(tmp_path, sleep=5)
    monkeypatch.setattr("yt_uploader.upload.upload_video", lambda *a, **k: "ok")

    sleep_calls = []
    monkeypatch.setattr("yt_uploader.upload.time.sleep", lambda s: sleep_calls.append(s))

    run(settings, account, dry_run=False, limit=None)

    # 3 videos -> sleep between #1->#2 and #2->#3, but not after #3
    assert len(sleep_calls) == 2


def test_run_preflight_failure_writes_no_meta_files(tmp_path, monkeypatch):
    account = _account(tmp_path, 2)
    settings = _settings(tmp_path)
    settings.uploader_binary = tmp_path / "does-not-exist"  # break the preflight check

    called = []
    monkeypatch.setattr("yt_uploader.upload.upload_video", lambda *a, **k: called.append(1) or "ok")

    exit_code = run(settings, account, dry_run=False, limit=None)

    assert exit_code == EXIT_FAILURES
    assert called == []  # never got as far as attempting an upload
    assert not settings.meta_dir.exists()


def test_run_dry_run_does_not_require_valid_binary(tmp_path, monkeypatch):
    account = _account(tmp_path, 1)
    settings = _settings(tmp_path)
    settings.uploader_binary = tmp_path / "does-not-exist"

    called = []
    monkeypatch.setattr("yt_uploader.upload.upload_video", lambda *a, **k: called.append(1) or "ok")

    exit_code = run(settings, account, dry_run=True, limit=None)

    assert exit_code == EXIT_OK
    assert called == []


def test_run_no_videos_returns_ok(tmp_path):
    account = _account(tmp_path, 0)
    settings = _settings(tmp_path)
    assert run(settings, account, dry_run=False, limit=None) == EXIT_OK


def test_run_missing_videos_dir_returns_ok_not_a_crash(tmp_path):
    settings = _settings(tmp_path)
    account = Account(
        name="en",
        videos_dir=tmp_path / "does-not-exist",
        client_secrets=tmp_path / "secrets.json",
        token_file=tmp_path / "token",
    )
    assert run(settings, account, dry_run=False, limit=None) == EXIT_OK


def test_run_daily_upload_limit_stops_early(tmp_path, monkeypatch):
    account = _account(tmp_path, n_videos=5, daily_limit=2)
    settings = _settings(tmp_path)
    monkeypatch.setattr("yt_uploader.upload.upload_video", lambda *a, **k: "ok")

    exit_code = run(settings, account, dry_run=False, limit=None)

    # Should return OK, not FAILURES (stopped early for config limit, not error)
    assert exit_code == EXIT_OK
    # Only 2 should be uploaded due to daily_limit
    uploaded = list((account.videos_dir / "old_videos").glob("*.mp4"))
    assert len(uploaded) == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
