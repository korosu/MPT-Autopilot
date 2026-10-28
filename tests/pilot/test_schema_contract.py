"""Contract tests: Autopilot's payload constants vs the MPT VideoParams schema.

B-6: the old tests compared Autopilot's whitelists to themselves, so a drift
from MoneyPrinterTurbo could never fail a test — B-1 (the invalid "None"
transition string) lived precisely in that gap. This file pins Autopilot's
side against a verified snapshot of MPT's schema
(tests/data/mpt_videoparams_snapshot.json), so a schema change or a typo in
our constants fails here instead of at render time.

The snapshot was verified against MoneyPrinterTurbo's app/models/schema.py
directly (MPT was not running, so /openapi.json could not be fetched — see
the _source field inside the snapshot). Regenerate it whenever the MPT
checkout is updated.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from mpt_autopilot.pilot.jobs import _KEY_ORDER
from mpt_autopilot.pilot.refill import (
    _VALID_ANIMATIONS,
    _VALID_BGM_TYPES,
    _VALID_CONCAT_MODES,
    _VALID_DISPLAY_MODES,
    _VALID_FIT_MODES,
    _VALID_IMAGE_MOTIONS,
    _VALID_TRANSITION_MODES,
)

_SNAPSHOT_PATH = Path(__file__).parents[1] / "data" / "mpt_videoparams_snapshot.json"
SNAPSHOT = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
FIELDS = SNAPSHOT["fields"]
ENUMS = SNAPSHOT["enums"]

# _KEY_ORDER entries that belong to Autopilot's job bookkeeping, not to the
# MPT payload (they are stripped by build_payload's meta_keys before submit).
_NON_MPT_KEYS = {"name", "enabled", "output_file"}

# Fields that do NOT exist in VideoParams but used to be written/sent (B-2,
# B-3) or look like they should (N-3: fps/resolution are hardcoded in MPT).
_GHOST_FIELDS = (
    "tts_server",
    "subtitle_background_enabled",
    "subtitle_background_color",
    "video_resolution",
    "fps",
)


def test_every_key_order_field_exists_in_the_mpt_schema():
    """The pilot's write whitelist must not drop or invent schema fields (B-7
    and B-2 classes of bugs: fields MPT has that we silently lose, and fields
    we write that MPT silently ignores)."""
    for key in _KEY_ORDER:
        if key in _NON_MPT_KEYS:
            continue
        assert key in FIELDS, (
            f"_KEY_ORDER lists {key!r}, but MPT VideoParams has no such field — "
            f"either a ghost key (B-2/B-3) or the snapshot is stale"
        )


def test_high_value_schema_fields_are_not_dropped_by_key_order():
    """B-7's exact regression: video_terms / match_materials_to_script /
    video_count / voice_volume exist in the schema, and the pilot must be
    able to write them into jobs."""
    for field in ("video_terms", "match_materials_to_script", "video_count", "voice_volume"):
        assert field in _KEY_ORDER, f"{field!r} exists in MPT but is missing from _KEY_ORDER (B-7)"
        assert field in FIELDS


def test_ghost_fields_are_absent_from_the_schema_and_the_whitelist():
    for ghost in _GHOST_FIELDS:
        assert ghost not in FIELDS, f"{ghost!r} appeared in the MPT snapshot — update this test"
        assert ghost not in _KEY_ORDER, f"{ghost!r} is not a VideoParams field (B-2/B-3)"


def test_transition_whitelist_matches_the_schema_enum():
    """B-1's contract: the string "None" is not an enum member; absence/null is
    how "no transition" is expressed."""
    assert _VALID_TRANSITION_MODES == set(ENUMS["video_transition_mode"])


def test_fit_mode_whitelist_matches_the_schema_enum():
    assert _VALID_FIT_MODES == set(ENUMS["video_fit_mode"])


def test_concat_mode_whitelist_matches_the_schema_enum():
    assert _VALID_CONCAT_MODES == set(ENUMS["video_concat_mode"])


def test_display_mode_whitelist_matches_the_schema_enum():
    assert _VALID_DISPLAY_MODES == set(ENUMS["subtitle_display_mode"])


def test_animation_whitelist_matches_the_schema_enum():
    assert _VALID_ANIMATIONS == set(ENUMS["subtitle_animation"])


def test_image_motion_whitelist_matches_the_schema_enum():
    """MPT 1.3.9's new ImageMotion enum: zoom = slow zoom (server default),
    static = no motion on still/AI images."""
    assert _VALID_IMAGE_MOTIONS == set(ENUMS["image_motion"])


def test_image_motion_is_writable_by_the_pilot():
    """image_motion is a real VideoParams field, so the pilot may write it and
    _validate_against_config must normalise into it (not drop it)."""
    assert "image_motion" in _KEY_ORDER
    assert "image_motion" in FIELDS


def test_bgm_types_match_what_get_bgm_file_honours():
    """Not a schema enum (plain str): pinned to the values get_bgm_file()
    actually honours — random picks a song, custom uses bgm_file, none skips."""
    assert _VALID_BGM_TYPES == {"random", "custom", "none"}


def test_jobs_example_defaults_stick_to_schema_fields():
    """The example file is the de-facto documentation of the payload; every
    key in its defaults: must be a real VideoParams field (or an Autopilot
    bookkeeping key). jobs.example.yaml:65-66 used to document ghost fields
    as real (B-2)."""
    example_path = Path(__file__).parents[2] / "jobs" / "jobs.example.yaml"
    parsed = yaml.safe_load(example_path.read_text(encoding="utf-8"))
    for key in parsed.get("defaults") or {}:
        assert key in FIELDS or key in _NON_MPT_KEYS or key == "voice", (
            f"jobs.example.yaml documents {key!r} in defaults:, but MPT "
            f"VideoParams has no such field"
        )


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
