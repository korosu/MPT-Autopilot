from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from yt_uploader.engine.uploader import UploadFailed, UploadLimitExceeded, upload_video


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["fake_uploader"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_quota_exceeded_is_detected(monkeypatch):
    monkeypatch.setattr(
        "yt_uploader.engine.uploader.subprocess.run",
        lambda *a, **k: _completed(1, stderr="uploadLimitExceeded: quota"),
    )
    with pytest.raises(UploadLimitExceeded):
        upload_video(Path("fake"), Path("v.mp4"), Path("m.json"), Path("s"), Path("t"))


def test_success_returns_output(monkeypatch):
    monkeypatch.setattr(
        "yt_uploader.engine.uploader.subprocess.run",
        lambda *a, **k: _completed(0, stdout="all good"),
    )
    output = upload_video(Path("fake"), Path("v.mp4"), Path("m.json"), Path("s"), Path("t"))
    assert "all good" in output


def test_generic_failure_is_upload_failed(monkeypatch):
    # Non-transient non-zero exit => fail fast: no retry, no sleep.
    sleeps = []
    monkeypatch.setattr(
        "yt_uploader.engine.uploader.subprocess.run",
        lambda *a, **k: _completed(1, stderr="boom"),
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


def test_retries_then_eventually_succeeds(monkeypatch):
    # Fails twice with a transient error, succeeds on the 3rd attempt.
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            return _completed(1, stderr="503 backendError")
        return _completed(0, stdout="success after 3 attempts")

    monkeypatch.setattr("yt_uploader.engine.uploader.subprocess.run", fake_run)
    output = upload_video(
        Path("fake"),
        Path("v.mp4"),
        Path("m.json"),
        Path("s"),
        Path("t"),
        sleep=lambda *_: None,
    )
    assert "success after 3 attempts" in output


def test_retries_exhausted_raises(monkeypatch):
    # Always-transient error => retries cap out and raise.
    monkeypatch.setattr(
        "yt_uploader.engine.uploader.subprocess.run",
        lambda *a, **k: _completed(1, stderr="503 service unavailable"),
    )
    with pytest.raises(UploadFailed) as exc_info:
        upload_video(
            Path("fake"),
            Path("v.mp4"),
            Path("m.json"),
            Path("s"),
            Path("t"),
            sleep=lambda *_: None,
        )
    assert "503" in str(exc_info.value)


def test_empty_output_non_transient_fails_fast(monkeypatch):
    # Empty output on non-zero exit is non-transient -> fail fast, no sleep
    sleeps = []
    monkeypatch.setattr(
        "yt_uploader.engine.uploader.subprocess.run",
        lambda *a, **k: _completed(1, stdout="", stderr=""),
    )
    with pytest.raises(UploadFailed) as exc_info:
        upload_video(
            Path("fake"),
            Path("v.mp4"),
            Path("m.json"),
            Path("s"),
            Path("t"),
            sleep=lambda s: sleeps.append(s),
        )
    assert sleeps == []
    assert "youtubeuploader exited with code" in str(exc_info.value)


def test_missing_binary_raises_upload_failed_not_a_crash(tmp_path):
    # No preflight check at this layer - upload_video() itself must not let
    # a raw OSError escape when the binary doesn't exist / isn't executable.
    missing = tmp_path / "does-not-exist"
    with pytest.raises(UploadFailed):
        upload_video(missing, Path("v.mp4"), Path("m.json"), Path("s"), Path("t"))


def test_oserror_chained_as_cause(monkeypatch):
    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr("yt_uploader.engine.uploader.subprocess.run", boom)
    with pytest.raises(UploadFailed) as exc_info:
        upload_video(Path("fake"), Path("v.mp4"), Path("m.json"), Path("s"), Path("t"))
    assert isinstance(exc_info.value.__cause__, OSError)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
