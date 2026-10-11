"""Tests for the batch-stage audit fixes (2026-10): M-22…M-32 and their L items.

Covered here:
  - M-22/M-23: upload_post privacy_level / privacy_status fallback semantics
  - M-26/M-27: state.add() lock + fsync, mkstemp temp names in _write_all
  - M-28: one build_payload() for both --dry-run and run_job
  - M-29: a job without output_file fails that job, not the whole run
  - M-31: log() routes through the shared Logger (level prefix, rotation)
  - poll interval comes from settings (audit L: hardcoded 10s)
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from mpt_autopilot.batch import state
from mpt_autopilot.batch.run import build_payload, log, run
from mpt_autopilot.batch.upload_post import UploadPostUnavailable, upload_video


@pytest.fixture(autouse=True)
def _close_batch_logger():
    """Close the named logger's handlers between tests.

    Logger keeps its RotatingFileHandler open for the whole process; without
    this, pytest's tmp_path cleanup on Windows cannot delete the log file of
    the previous test.
    """
    yield
    lg = logging.getLogger("mpt_autopilot.batch")
    for handler in list(lg.handlers):
        lg.removeHandler(handler)
        handler.close()


# ── upload_post privacy semantics (M-22, M-23) ───────────────────────────────


def _up_settings(**overrides):
    base = {
        "upload_post_api_key": "key",
        "upload_post_username": "user",
        "upload_post_platforms": ["youtube", "tiktok"],
        "upload_post_youtube_privacy_status": "public",
        "upload_post_privacy_level": None,
        "upload_post_contains_synthetic_media": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _upload(tmp_path: Path, settings: SimpleNamespace, **kwargs):
    """Run upload_video against a fake shared client; return (result, form data)."""
    video = tmp_path / "v.mp4"
    video.write_bytes(b"VID")
    with patch("mpt_autopilot.batch.upload_post.get_shared_client") as gsc:
        client = MagicMock()
        gsc.return_value = client
        client.post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"success": True, "request_id": "r1"},
        )
        result = upload_video(video, "T", "D", ["#tag"], settings, **kwargs)  # type: ignore[arg-type]
    data = client.post.call_args.kwargs["data"]
    return result, data


def test_privacy_level_defaults_to_public_to_everyone_for_public(tmp_path: Path):
    """A public YouTube status keeps the historical upload-post default."""
    result, data = _upload(tmp_path, _up_settings())
    assert result["success"] is True
    assert data["privacy_level"] == "PUBLIC_TO_EVERYONE"
    # containsSyntheticMedia comes from config, default true (B-9/R9.6).
    assert data["containsSyntheticMedia"] == "true"


def test_contains_synthetic_media_false_is_sent(tmp_path: Path):
    """B-9/R9.6: the AI-use disclosure was hard-coded "true"; a sender who
    discloses nothing must be able to say so."""
    _, data = _upload(tmp_path, _up_settings(upload_post_contains_synthetic_media=False))
    assert data["containsSyntheticMedia"] == "false"


def test_privacy_level_passes_configured_value_through(tmp_path: Path):
    """A configured privacy_level is sent verbatim (values are platform-side)."""
    _, data = _upload(tmp_path, _up_settings(upload_post_privacy_level="PRIVATE_TO_SELF"))
    assert data["privacy_level"] == "PRIVATE_TO_SELF"


def test_unlisted_without_privacy_level_and_tiktok_refuses(tmp_path: Path):
    """The old behaviour published TikTok publicly on an unlisted YouTube
    request; now the upload refuses instead of guessing the user's intent."""
    s = _up_settings(upload_post_youtube_privacy_status="unlisted")
    with pytest.raises(UploadPostUnavailable, match="privacy_level"):
        _upload(tmp_path, s)


def test_unlisted_with_youtube_only_platform_needs_no_privacy_level(tmp_path: Path):
    """privacy_level only governs non-YouTube platforms — with none of them
    in the request, an unlisted YouTube upload needs no extra setting."""
    s = _up_settings(
        upload_post_youtube_privacy_status="unlisted",
        upload_post_platforms=["youtube"],
    )
    _, data = _upload(tmp_path, s)
    assert data["privacyStatus"] == "unlisted"


def test_privacy_status_none_falls_back_to_settings(tmp_path: Path):
    """M-23: the default privacy_status is None, so the configured
    youtube_privacy_status is actually reachable."""
    s = _up_settings(
        upload_post_youtube_privacy_status="private",
        upload_post_platforms=["youtube"],
    )
    _, data = _upload(tmp_path, s)
    assert data["privacyStatus"] == "private"


# ── build_payload (M-28) ──────────────────────────────────────────────────────


def test_build_payload_strips_meta_keys_and_merges_defaults():
    defaults = {"video_language": "en", "aspect_ratio": "9:16"}
    job = {
        "name": "n",
        "enabled": True,
        "output_file": "v.mp4",
        "max_retries": 9,
        "retry_delay_seconds": 1,
        "subtitle_enabled": True,
        "aspect_ratio": "16:9",
    }
    payload = build_payload(job, defaults, {})
    assert payload["subtitle_enabled"] is True
    assert payload["aspect_ratio"] == "16:9"  # the job wins over the default
    assert payload["video_language"] == "en"  # the default survives
    for meta in ("name", "enabled", "output_file", "max_retries", "retry_delay_seconds"):
        assert meta not in payload


def test_build_payload_raises_key_error_for_unknown_voice_alias():
    """--dry-run and run_job share build_payload, so both fail identically on
    an unknown voice alias instead of only the real run."""
    with pytest.raises(KeyError, match="voice alias"):
        build_payload({"voice": "no_such_alias"}, {}, {})


# ── jobs without output_file (M-29) ───────────────────────────────────────────


def _run_settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        api_url="http://127.0.0.1:1",
        output_dir=tmp_path / "exports",
        voice_pool={},
        log_file=tmp_path / "batch.log",
        log_max_bytes=100_000,
        upload_post_enabled=False,
        cache_cleanup_enabled=False,
        cache_cleanup_interval=0,
        max_consecutive_failures=3,
        telegram_token="",
        telegram_chat_id="",
        telegram_prefix="[test]",
    )


def test_run_fails_job_without_output_file_not_the_run(tmp_path: Path, capsys):
    """A hand-edited jobs.yaml entry missing output_file used to kill the
    whole run with a traceback; it must fail that one job instead."""
    jobs_path = tmp_path / "jobs.yaml"
    jobs_path.write_text("jobs:\n  - name: broken\n    enabled: true\n", encoding="utf-8")

    with patch("mpt_autopilot.batch.run.health_check", return_value=True):
        with patch("mpt_autopilot.batch.run.notify.alert"):
            with patch("mpt_autopilot.batch.run.run_job") as mock_run_job:
                code = run(
                    jobs_path,
                    _run_settings(tmp_path),  # type: ignore[arg-type]
                    dry_run=False,
                    seen_override=tmp_path / "seen.txt",
                )

    assert code == 1  # the one attempted job failed
    assert mock_run_job.call_count == 0  # never reached the API
    assert "no output_file" in capsys.readouterr().out
    # The run lock was released on the way out.
    assert not (tmp_path / "seen.run.lock").exists()


def test_dry_run_reports_job_without_output_file(tmp_path: Path, capsys):
    jobs_path = tmp_path / "jobs.yaml"
    jobs_path.write_text("jobs:\n  - name: broken\n", encoding="utf-8")

    code = run(
        jobs_path,
        _run_settings(tmp_path),  # type: ignore[arg-type]
        dry_run=True,
        seen_override=tmp_path / "seen.txt",
    )
    assert code == 0
    assert "error     broken - no output_file" in capsys.readouterr().out


# ── log() through the shared Logger (M-31) ───────────────────────────────────


def test_log_routes_through_shared_logger(tmp_path: Path, capsys):
    settings = SimpleNamespace(log_file=tmp_path / "b.log", log_max_bytes=100_000)
    log("hello", settings)  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert "[INFO] hello" in out
    assert (tmp_path / "b.log").read_text(encoding="utf-8").endswith("hello\n")


def test_log_to_file_false_prints_without_creating_a_file(tmp_path: Path, capsys):
    settings = SimpleNamespace(log_file=tmp_path / "b.log", log_max_bytes=100_000)
    log("preview line", settings, to_file=False)  # type: ignore[arg-type]
    assert "preview line" in capsys.readouterr().out
    assert not (tmp_path / "b.log").exists()


# ── poll interval from settings ───────────────────────────────────────────────


def test_wait_for_task_sleeps_the_configured_interval(monkeypatch):
    """The 10s poll interval is configurable (audit L), so long-polling
    setups can slow the request rate without forking the code."""
    import mpt_autopilot.batch.api as api_mod
    from mpt_autopilot.batch.api import wait_for_task

    settings = MagicMock(
        api_url="http://x",
        max_wait_seconds=30,
        stuck_threshold_seconds=60,
        poll_interval_seconds=7,
    )
    sleeps: list[object] = []
    monkeypatch.setattr(api_mod.time, "sleep", lambda s: sleeps.append(s))

    def _respond(progress: int, st: int) -> MagicMock:
        return MagicMock(json=lambda: {"data": {"progress": progress, "state": st}})

    with patch("mpt_autopilot.batch.api.get_shared_client") as gsc:
        client = MagicMock()
        gsc.return_value = client
        client.get.side_effect = [_respond(5, 4), _respond(100, 1)]
        wait_for_task("t", settings)

    assert sleeps == [7]


# ── R6: a rejected payload is not retried ────────────────────────────────────


def test_rejected_payload_fails_the_job_without_retries(tmp_path: Path):
    """B-1's hidden cost: a 4xx retried 3×180 s per job, then the batch died
    on max_consecutive_failures. The submission error must stop at one try."""
    from mpt_autopilot.batch import run as run_mod
    from mpt_autopilot.batch.api import PayloadRejected

    settings = SimpleNamespace(
        max_retries=3,
        retry_delay_seconds=0,
        upload_post_enabled=False,
        telegram_token="",
        telegram_chat_id="",
        telegram_prefix="test",
        log_file=tmp_path / "batch.log",
        log_max_bytes=10**6,
        mpt_storage=tmp_path / "storage",
    )
    calls: list[int] = []

    def boom(payload, s):
        calls.append(1)
        raise PayloadRejected(
            "MPT rejected the submission (HTTP 400): "
            "['body', 'video_transition_mode']: Input should be 'Shuffle'..."
        )

    with patch.object(run_mod, "submit_job", side_effect=boom):
        ok = run_mod.run_job(
            {"name": "j", "output_file": "o.mp4"},
            {},
            {},
            settings,  # type: ignore[arg-type]
            tmp_path / "ip.txt",
        )

    assert ok is False
    assert len(calls) == 1  # exactly one attempt — no retries for a 4xx


# ── state.add lock/fsync + mkstemp temp names (M-26, M-27) ────────────────────


def test_state_add_keeps_every_recorded_entry(tmp_path: Path):
    p = tmp_path / "in_progress.txt"
    state.add(p, "a.mp4", "t1", 1)
    state.add(p, "b.mp4", "t2", 1)
    assert [e["output_file"] for e in state.list_all(p)] == ["a.mp4", "b.mp4"]
    # The lock is a context manager: it must not outlive the call.
    assert not (tmp_path / "in_progress.txt.lock").exists()


def test_state_write_all_leaves_no_temp_file(tmp_path: Path):
    """M-27: the temp file name is unique (mkstemp), and none is left behind."""
    p = tmp_path / "in_progress.txt"
    state._write_all(p, [{"output_file": "a.mp4", "task_id": "t1", "attempt": 1}])
    assert "a.mp4" in p.read_text(encoding="utf-8")
    assert list(tmp_path.glob(".*")) == []
