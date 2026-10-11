"""Tests for the uploader audit fixes (2026-10): M-42…M-49 and their L items.

Covered here:
  - M-42: crash-recovery finishes every pending row in one pass
  - M-43/M-44/M-45: config validation (privacy_status / tags / empty account body)
  - M-46: ledger_path is configurable
  - M-47: a sidecar that is valid JSON but not an object is a clean ValueError
  - M-48/M-49: transient markers match on word boundaries; a timeout is permanent
  - L: mark_started keeps the original created_at; meta scratch files are cleaned
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mpt_autopilot.uploader.ledger import (
    mark_started,
    mark_uploaded,
    open_ledger,
    sha256_file,
)
from mpt_autopilot.uploader.metadata import load_meta
from mpt_autopilot.uploader.run import EXIT_OK, run
from mpt_autopilot.uploader.settings import Account, Defaults, Settings, load_settings
from mpt_autopilot.uploader.uploader import UploadFailed, upload_video
from tests.uploader.helpers import make_uploader_binary


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["fake_uploader"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _load(tmp_path: Path, config_body: str, accounts_body: str = "accounts: {}\n") -> Settings:
    (tmp_path / "accounts.yaml").write_text(accounts_body, encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(config_body, encoding="utf-8")
    return load_settings(config_path=config_path)


# ── M-48 / M-49: transient markers and the timeout ───────────────────────────


def test_timeout_is_permanent_not_retried(monkeypatch):
    """A kill on timeout proves nothing about finalize; the old code retried it
    via its own "timeout" marker — tautologically transient (M-49)."""
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        raise subprocess.TimeoutExpired(cmd=["fake"], timeout=600)

    monkeypatch.setattr("mpt_autopilot.uploader.uploader.subprocess.run", fake_run)
    sleeps: list[object] = []

    with pytest.raises(UploadFailed, match="not retried"):
        upload_video(
            Path("fake"),
            Path("v.mp4"),
            Path("m.json"),
            Path("s"),
            Path("t"),
            sleep=lambda s: sleeps.append(s),
        )
    assert calls["n"] == 1
    assert sleeps == []


@pytest.mark.parametrize(
    "output",
    [
        "uploading 1503 of 50332 bytes",  # "503" inside numbers
        "timeout waiting for encoder",  # the word "timeout" alone
        "deadline exceeded after 503 ms",  # both old substrings, no real marker
    ],
)
def test_stray_marker_substrings_are_not_transient(monkeypatch, output):
    """M-48: the old raw-substring match treated these as retryable; only
    Google API error reasons count now, on word boundaries."""
    sleeps: list[object] = []
    monkeypatch.setattr(
        "mpt_autopilot.uploader.uploader.subprocess.run",
        lambda *a, **k: _completed(1, stdout=output),
    )
    with pytest.raises(UploadFailed):
        upload_video(
            Path("fake"),
            Path("v.mp4"),
            Path("m.json"),
            Path("s"),
            Path("t"),
            sleep=lambda s: sleeps.append(s),
        )
    assert sleeps == []


def test_real_api_reason_is_still_transient(monkeypatch):
    """backendError as a whole word must still retry — the marker set shrank,
    it did not disappear."""
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        if calls["n"] < 2:
            return _completed(1, stderr="googleapi: backendError")
        return _completed(0, stdout="ok")

    monkeypatch.setattr("mpt_autopilot.uploader.uploader.subprocess.run", fake_run)
    output = upload_video(
        Path("fake"), Path("v.mp4"), Path("m.json"), Path("s"), Path("t"), sleep=lambda _: None
    )
    assert output == "ok"
    assert calls["n"] == 2


# ── M-43 / M-44 / M-45 / M-46: settings validation ───────────────────────────


def test_config_privacy_status_is_validated(tmp_path):
    with pytest.raises(ValueError, match="privacy_status"):
        _load(tmp_path, "uploader:\n  defaults:\n    privacy_status: friends\n")


def test_config_tags_scalar_is_wrapped_not_split_into_chars(tmp_path):
    """`tags: shorts` used to become ['s','h','o','r','t','s'] (M-44)."""
    settings = _load(tmp_path, "uploader:\n  defaults:\n    tags: shorts\n")
    assert settings.defaults.tags == ["shorts"]


def test_config_tags_rejects_a_mapping(tmp_path):
    with pytest.raises(ValueError, match="tags"):
        _load(tmp_path, "uploader:\n  defaults:\n    tags:\n      a: b\n")


def test_account_without_body_is_a_named_error(tmp_path):
    """`accounts: {main:}` parses as {main: None}; raw.get() on None used to
    crash with an AttributeError outside the except clause (M-45)."""
    with pytest.raises(ValueError, match="no body"):
        _load(tmp_path, "", accounts_body="accounts:\n  main:\n")


def test_ledger_path_is_configurable(tmp_path):
    settings = _load(tmp_path, "uploader:\n  ledger_path: ./ledgers/custom.sqlite\n")
    assert settings.ledger_path.name == "custom.sqlite"
    assert settings.ledger_path.parent == tmp_path / "ledgers"


def test_ledger_path_defaults_next_to_config(tmp_path):
    settings = _load(tmp_path, "")
    assert settings.ledger_path == tmp_path / "yt-uploader-ledger.sqlite"


# ── M-47: non-object sidecar ──────────────────────────────────────────────────


def test_sidecar_top_level_array_is_a_clean_value_error(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"VID")
    (tmp_path / "v.json").write_text("[1, 2]", encoding="utf-8")

    with pytest.raises(ValueError, match="JSON object"):
        load_meta(video, Defaults(), account_name="en")


# ── L: mark_started keeps the original created_at ────────────────────────────


def test_mark_started_does_not_reset_created_at(tmp_path):
    conn = open_ledger(tmp_path / "ledger.sqlite")
    try:
        mark_started(conn, "en", "hash-1", "a.mp4")
        first = conn.execute(
            "SELECT created_at FROM uploads WHERE account = 'en' AND content_hash = 'hash-1'"
        ).fetchone()[0]
        mark_started(conn, "en", "hash-1", "a.mp4")  # retried run
        second = conn.execute(
            "SELECT created_at FROM uploads WHERE account = 'en' AND content_hash = 'hash-1'"
        ).fetchone()[0]
        assert first == second
    finally:
        conn.close()


# ── M-42: recovery finishes every pending row in one pass ─────────────────────


def _account(tmp_path: Path, n_videos: int) -> Account:
    videos_dir = tmp_path / "videos"
    videos_dir.mkdir(exist_ok=True)
    for i in range(n_videos):
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
    return Settings(
        uploader_binary=make_uploader_binary(tmp_path),
        meta_dir=tmp_path / "meta",
        sleep_between_uploads=0,
        uploaded_dir_name="old_videos",
        defaults=Defaults(),
        telegram_token="",
        telegram_chat_id="",
        telegram_prefix="test",
        ledger_path=tmp_path / "ledger.sqlite",
        accounts={},
        keep_uploaded=True,
    )


def test_recovery_moves_all_pending_rows_in_one_pass(tmp_path, monkeypatch, capsys):
    """mark_moved() commits on the same connection mid-iteration; without
    materialising the rows SQLite owes no promise about which rows the cursor
    still has left (M-42)."""
    account = _account(tmp_path, n_videos=3)
    settings = _settings(tmp_path)

    conn = open_ledger(settings.ledger_path)
    try:
        for name in ("video_0.mp4", "video_1.mp4", "video_2.mp4"):
            content_hash = sha256_file(account.videos_dir / name)
            mark_started(conn, account.name, content_hash, name)
            mark_uploaded(conn, account.name, content_hash)
    finally:
        conn.close()

    # A real upload must not happen for these: they are already uploaded.
    upload_calls = []
    monkeypatch.setattr(
        "mpt_autopilot.uploader.run.upload_video",
        lambda *a, **k: upload_calls.append(a) or "ok",
    )

    assert run(settings, account, dry_run=False, limit=None) == EXIT_OK
    assert upload_calls == []
    out = capsys.readouterr().out
    assert out.count("recovering: video_") == 3
    uploaded = list((account.videos_dir / "old_videos").glob("*.mp4"))
    assert len(uploaded) == 3


def test_meta_scratch_file_is_removed_after_success(tmp_path, monkeypatch):
    """The metaJSON is regenerated from the sidecar on every attempt; leaving
    one per upload littered meta_dir forever (audit L)."""
    account = _account(tmp_path, n_videos=1)
    settings = _settings(tmp_path)
    monkeypatch.setattr("mpt_autopilot.uploader.run.upload_video", lambda *a, **k: "ok")

    assert run(settings, account, dry_run=False, limit=None) == EXIT_OK
    assert settings.meta_dir.exists()
    assert list(settings.meta_dir.glob("*.json")) == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
