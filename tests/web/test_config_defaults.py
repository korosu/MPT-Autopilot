"""
Tests for the config → form-field mapping behind "Fill from config".

The mapping is a mirror of each stage's own resolution order, so the pins that
matter are the values: a config written the way config.example.yaml writes one
must produce exactly the fields the stage would use. Nothing here may raise on
a config with whole sections missing — the page renders for a fresh install.

"""

from __future__ import annotations

from pathlib import Path

from mpt_autopilot import config as shared_config
from mpt_autopilot.web import config_defaults

# Every key the module maps, written the way config.example.yaml writes them.
FULL = """
langs:
  en:
    label: English
paths:
  jobs_dir: ./jobs
  seen_dir: ./state
  videos_dir: ./exports
pilot:
  scan_dirs:
    - ./videos
    - ./exports
  generation:
    count: 5
    threshold: 3
enricher:
  platform: youtube
  batch_size: 7
uploader:
  accounts_file: ./accounts.yaml
"""


def _cfg(tmp_path: Path, text: str) -> shared_config.Config:
    config = tmp_path / "config.yaml"
    config.write_text(text, encoding="utf-8")
    return shared_config.load(config, env_path=config, reload=True)


def test_a_config_that_sets_everything_fills_every_mapped_field(tmp_path):
    cfg = _cfg(tmp_path, FULL)

    assert config_defaults.stage_defaults(cfg) == {
        "refill": {
            "jobs_dir": str(tmp_path / "jobs"),
            # pilot.seen_dir is unset, so the shared paths: block answers
            # before the jobs_dir fallback would — pilot/settings.py:184-185.
            "seen_dir": str(tmp_path / "state"),
            "count": "5",
            "threshold": "3",
        },
        "init-seen": {
            "dirs": "./videos, ./exports",
            "jobs_dir": str(tmp_path / "jobs"),
            "seen_dir": str(tmp_path / "state"),
        },
        "batch": {
            # No batch.jobs, so jobs_dir + the plain jobs.yaml — run.py:1026.
            "jobs": str(tmp_path / "jobs" / "jobs.yaml"),
            # No --lang on the web form's batch, so the plain seen.txt.
            "seen": str(tmp_path / "state" / "seen.txt"),
        },
        "enrich": {
            "dir": str(tmp_path / "exports"),
            "platform": "youtube",
            "batch_size": "7",
        },
        "upload": {"accounts_file": str(tmp_path / "accounts.yaml")},
        # run's --refill-count / --refill-threshold read the same generation
        # keys refill does, so a filled run tab matches a filled refill tab.
        "run": {"refill_count": "5", "refill_threshold": "3"},
        # validate registers no flag the config speaks to.
    }


def test_a_config_with_only_langs_fills_nothing(tmp_path):
    cfg = _cfg(tmp_path, "langs:\n  en:\n    label: English\n")

    assert config_defaults.stage_defaults(cfg) == {
        "refill": {},
        "init-seen": {},
        "batch": {},
        "enrich": {},
        "upload": {},
        "run": {},
    }


def test_the_shared_paths_block_fills_a_silent_stage_section(tmp_path):
    """No pilot:/batch:/enricher: section at all — `paths:` still answers."""
    cfg = _cfg(tmp_path, "langs: {}\npaths:\n  jobs_dir: ./jobs\n  videos_dir: ./exports\n")

    defaults = config_defaults.stage_defaults(cfg)
    assert defaults["refill"]["jobs_dir"] == str(tmp_path / "jobs")
    assert defaults["batch"]["jobs"] == str(tmp_path / "jobs" / "jobs.yaml")
    assert defaults["enrich"]["dir"] == str(tmp_path / "exports")


def test_a_stage_own_value_beats_the_shared_block(tmp_path):
    cfg = _cfg(
        tmp_path,
        "langs: {}\npaths:\n  jobs_dir: ./shared\npilot:\n  jobs_dir: ./pilot-jobs\n",
    )

    assert config_defaults.stage_defaults(cfg)["refill"]["jobs_dir"] == str(tmp_path / "pilot-jobs")


def test_pilot_seen_dir_falls_back_to_jobs_dir(tmp_path):
    """Neither pilot.seen_dir nor paths.seen_dir — settings.py:184-185 falls back."""
    cfg = _cfg(tmp_path, "langs: {}\npaths:\n  jobs_dir: ./jobs\n")

    assert config_defaults.stage_defaults(cfg)["refill"]["seen_dir"] == str(tmp_path / "jobs")
    assert config_defaults.stage_defaults(cfg)["init-seen"]["seen_dir"] == str(tmp_path / "jobs")


def test_batch_prefers_a_direct_jobs_path(tmp_path):
    cfg = _cfg(
        tmp_path,
        "langs: {}\nbatch:\n  jobs_dir: ./jobs\n  jobs: ./queue/jobs_ru.yaml\n",
    )

    # run.py:1026 — settings.jobs wins outright, no jobs.yaml appended.
    assert config_defaults.stage_defaults(cfg)["batch"]["jobs"] == str(
        tmp_path / "queue" / "jobs_ru.yaml"
    )


def test_batch_seen_comes_from_batch_or_the_shared_block(tmp_path):
    cfg = _cfg(tmp_path, "langs: {}\nbatch:\n  seen_dir: ./batch-state\n")
    assert config_defaults.stage_defaults(cfg)["batch"]["seen"] == str(
        tmp_path / "batch-state" / "seen.txt"
    )

    cfg = _cfg(tmp_path, "langs: {}\npaths:\n  seen_dir: ./shared-state\n")
    assert config_defaults.stage_defaults(cfg)["batch"]["seen"] == str(
        tmp_path / "shared-state" / "seen.txt"
    )


def test_scan_dirs_join_the_way_the_form_expects(tmp_path):
    cfg = _cfg(tmp_path, "langs: {}\npilot:\n  scan_dirs:\n    - ./videos\n    - ./exports\n")
    assert config_defaults.stage_defaults(cfg)["init-seen"]["dirs"] == "./videos, ./exports"


def test_a_bare_string_scan_dirs_stays_a_bare_string(tmp_path):
    cfg = _cfg(tmp_path, "langs: {}\npilot:\n  scan_dirs: ./videos\n")
    assert config_defaults.stage_defaults(cfg)["init-seen"]["dirs"] == "./videos"


def test_enrich_platform_is_lower_cased_like_the_stage_does(tmp_path):
    cfg = _cfg(tmp_path, "langs: {}\nenricher:\n  platform: YouTube\n")
    assert config_defaults.stage_defaults(cfg)["enrich"]["platform"] == "youtube"


def test_garbage_values_are_ignored_not_guessed(tmp_path):
    """
    The page must not turn a typo into a value the stage would reject: a field
    the config cannot answer for stays empty and the run keeps its default.
    """
    cfg = _cfg(
        tmp_path,
        "langs: {}\n"
        "pilot:\n"
        "  scan_dirs: 5\n"
        "  generation:\n"
        "    count: many\n"
        "enricher:\n"
        "  platform: 7\n"
        "  batch_size: [1]\n",
    )

    assert config_defaults.stage_defaults(cfg) == {
        "refill": {},
        "init-seen": {},
        "batch": {},
        "enrich": {},
        "upload": {},
        "run": {},
    }


def test_every_filled_path_is_absolute(tmp_path):
    """
    The web child runs with cwd set to the config's directory, but the values
    are also what a user reads on screen — absolute is the only spelling that
    means the same thing in both places.
    """
    defaults = config_defaults.stage_defaults(_cfg(tmp_path, FULL))
    for stage, values in defaults.items():
        for dest, value in values.items():
            if dest in {
                "count",
                "threshold",
                "dirs",
                "platform",
                "batch_size",
                "refill_count",
                "refill_threshold",
            }:
                continue
            assert Path(value).is_absolute(), f"{stage}.{dest} = {value}"


def test_code_defaults_stay_unfilled(tmp_path):
    """
    Only config-supplied values are offered. 21/10 (generation count/threshold),
    the accounts.yaml-next-to-config.yaml fallback and the "current directory"
    jobs path are argparse/stage defaults — the input's placeholder already
    shows them, so filling them would double up the truth.
    """
    cfg = _cfg(tmp_path, "langs:\n  en:\n    label: English\n")
    defaults = config_defaults.stage_defaults(cfg)
    assert "count" not in defaults["refill"]
    assert "threshold" not in defaults["refill"]
    assert "accounts_file" not in defaults["upload"]
    assert "jobs" not in defaults["batch"]
