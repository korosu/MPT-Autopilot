"""
tests/batch/test_paths_traversal.py — untrusted values must not reach filesystem
sinks that delete or overwrite outside the expected directories.

Two entry points, both untrusted:

  task_id     — from the MPT API response (api.submit_job) or from the
                in_progress.txt crash-recovery file. Interpolated into
                mpt_storage/tasks/<task_id>, which cleanup_task() deletes.
  output_file — from jobs.yaml, which the user edits by hand. The pilot stage
                sanitises its own output, but hand-written or hand-imported
                entries do not get that treatment. Used as a copy2 destination.

The MPT API is unauthenticated and api_url is user-configurable, so a
compromised or misconfigured endpoint (or a hand-edited jobs.yaml) could
otherwise delete the whole mpt_storage tree or overwrite config.yaml.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from mpt_autopilot.batch import api as batch_api
from mpt_autopilot.batch import run as batch_run


@pytest.fixture()
def storage_tree(tmp_path: Path):
    storage = tmp_path / "storage"
    (tasks := storage / "tasks").mkdir(parents=True)
    (tasks / "LEGIT-ABC123").mkdir()
    (tasks / "LEGIT-ABC123" / "final-1.mp4").write_bytes(b"x")
    (cache := storage / "cache_videos").mkdir()
    (cache / "clip.mp4").write_bytes(b"y")
    (sibling := tmp_path / "sibling").mkdir()
    (sibling / "precious.txt").write_text("keep", encoding="utf-8")
    (exports := tmp_path / "exports").mkdir()
    (outside := tmp_path / "outside" / "d").mkdir(parents=True)
    (outside / "victim.txt").write_text("original\n", encoding="utf-8")

    settings = SimpleNamespace(
        mpt_storage=storage,
        output_dir=exports,
        log_file=tmp_path / "nop.log",
        log_max_bytes=10**7,
        telegram_token="",
        telegram_chat_id="",
        telegram_prefix="test",
    )
    batch_run.log = lambda msg, s, to_file=True: None
    return SimpleNamespace(
        settings=settings,
        storage=storage,
        tasks=tasks,
        cache=cache,
        sibling=sibling,
        exports=exports,
        outside=outside,
    )


# ── H1: task_id -> shutil.rmtree ────────────────────────────────────────────


@pytest.mark.parametrize("evil", ["..", "..\\..", "tasks/..", "", "a/b", "/abs", "has space"])
def test_task_id_traversal_is_refused(storage_tree, evil: str) -> None:
    with pytest.raises(ValueError):
        batch_run.cleanup_task(evil, storage_tree.settings)


def test_task_id_traversal_deletes_nothing(storage_tree) -> None:
    for evil in ("..", "..\\.."):
        with pytest.raises(ValueError):
            batch_run.cleanup_task(evil, storage_tree.settings)
    assert storage_tree.storage.exists(), "cleanup_task deleted the mpt_storage tree"
    assert (storage_tree.cache / "clip.mp4").exists()
    assert (storage_tree.sibling / "precious.txt").exists()


def test_legitimate_task_id_still_cleans_up(storage_tree) -> None:
    batch_run.cleanup_task("LEGIT-ABC123", storage_tree.settings)
    assert not (storage_tree.tasks / "LEGIT-ABC123").exists()
    assert (storage_tree.cache / "clip.mp4").exists()


def test_validate_task_id_accepts_safe_ids() -> None:
    for good in ("abc123", "ABC-DEF_1.2", "a", "0x-dead_beef.42"):
        assert batch_api.validate_task_id(good) == good


_BAD_IDS = ["..", "a/b", "a\\b", "/abs", "has space", "has\ttab", "", "a" * 200, None, 5]


@pytest.mark.parametrize("bad", _BAD_IDS)
def test_validate_task_id_rejects_unsafe_ids(bad) -> None:
    with pytest.raises(ValueError):
        batch_api.validate_task_id(bad)


def test_submit_job_validates_the_task_id_from_the_api(monkeypatch) -> None:
    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": {"task_id": ".."}}

    settings = SimpleNamespace(api_url="http://127.0.0.1:1")
    monkeypatch.setattr(batch_api.httpx, "post", lambda *a, **k: FakeResponse())
    with pytest.raises(ValueError, match="unsafe task_id"):
        batch_api.submit_job({}, settings)  # type: ignore[arg-type]


# ── H2: output_file -> shutil.copy2 destination ─────────────────────────────


@pytest.fixture()
def finished_task(storage_tree):
    """A real finished task so copy_result has a source to copy from."""
    task_dir = storage_tree.tasks / "T9"
    task_dir.mkdir()
    (task_dir / "final-1.mp4").write_bytes(b"VIDEO")
    return task_dir


@pytest.mark.parametrize(
    "evil",
    [
        "../outside/d/pwned.mp4",
        "..\\..\\outside\\d\\pwned.mp4",
        "/tmp/abs.mp4",
        "C:\\Windows\\Temp\\abs.mp4",
    ],
)
def test_output_file_traversal_is_refused(storage_tree, finished_task, evil: str) -> None:
    with pytest.raises(ValueError):
        batch_run.copy_result({"task_id": "T9", "videos": []}, evil, storage_tree.settings)


def test_output_file_traversal_writes_nothing(storage_tree, finished_task) -> None:
    with pytest.raises(ValueError):
        batch_run.copy_result(
            {"task_id": "T9", "videos": []}, "../outside/d/pwned.mp4", storage_tree.settings
        )
    victim = storage_tree.outside / "victim.txt"
    assert victim.read_text(encoding="utf-8") == "original\n"


def test_legitimate_output_file_still_copies(storage_tree, finished_task) -> None:
    task_id = batch_run.copy_result(
        {"task_id": "T9", "videos": []}, "good_video.mp4", storage_tree.settings
    )
    assert (storage_tree.exports / "good_video.mp4").read_bytes() == b"VIDEO"
    assert task_id == "T9"


def test_empty_output_file_is_refused(storage_tree, finished_task) -> None:
    with pytest.raises(ValueError):
        batch_run.copy_result({"task_id": "T9", "videos": []}, "  ", storage_tree.settings)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
