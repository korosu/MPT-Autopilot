"""
tests/uploader/test_metadata_robustness.py — load_meta() must tolerate a
malformed sidecar instead of crashing the upload run.

A sidecar <video>.json sits next to every video and is either written by
`mpt enrich` or edited by hand. load_meta() documents that "Any field can be
omitted" — implying tolerant parsing. Instead, a non-string `tags` entry
raises AttributeError, which propagates out of uploader.run() and out of
execute() (execute() only guards get_account(), not run()), so
`mpt upload --account en` dies with a raw traceback.

Also: an empty-string tag is passed through to YouTube metadata unfiltered.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from mpt_autopilot.uploader.metadata import load_meta
from mpt_autopilot.uploader.settings import Defaults


@pytest.fixture()
def defaults() -> Defaults:
    return Defaults()


def _video_with_sidecar(tmp_path: Path, stem: str, sidecar: dict) -> Path:
    video = tmp_path / f"{stem}.mp4"
    video.write_bytes(b"")
    video.with_suffix(".json").write_text(json.dumps(sidecar), encoding="utf-8")
    return video


@pytest.mark.parametrize(
    "payload",
    [
        {"tags": [123, 456]},
        {"tags": [None, "#ok"]},
        {"tags": [1, None, "", "  ", "#ok", "#ok2"]},
        {"tags": [1, None, ""]},  # every entry unusable
        {"tags": []},  # empty list
        {"tags": "not-a-list"},  # wrong type entirely
        {"tags": 42},
    ],
)
def test_non_string_tag_does_not_crash(tmp_path: Path, defaults: Defaults, payload: dict) -> None:
    """
    A non-string or unusable tag entry used to raise AttributeError inside
    _apply_tags_placement: `'int' object has no attribute 'startswith'`.
    load_meta() sits outside the per-video try/except in uploader.run(), so one
    bad sidecar halted every remaining upload for the account and the summary
    Telegram alert never fired.
    """
    video = _video_with_sidecar(tmp_path, "v", {"title": "T", **payload})
    meta = load_meta(video, defaults, account_name="")
    assert all(isinstance(t, str) for t in meta.tags)
    assert all(t.strip() for t in meta.tags), "blank tags must not reach YouTube"
    # defaults.tags are always present — they are the account's baseline
    assert defaults.tags and all(isinstance(t, str) for t in defaults.tags)


def test_empty_string_tag_is_dropped(tmp_path: Path, defaults: Defaults) -> None:
    video = _video_with_sidecar(tmp_path, "v", {"title": "T", "tags": ["", "#ok"]})
    meta = load_meta(video, defaults, account_name="")
    assert "" not in meta.tags, "an empty tag would be uploaded to YouTube"


def test_tags_placement_strips_hash_but_keeps_tag_body(tmp_path: Path, defaults: Defaults) -> None:
    defaults.hashtag_placement = "tags"
    video = _video_with_sidecar(tmp_path, "v", {"title": "T", "tags": ["#shorts", "#history"]})
    meta = load_meta(video, defaults, account_name="")
    assert "shorts" in meta.tags and "history" in meta.tags


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
