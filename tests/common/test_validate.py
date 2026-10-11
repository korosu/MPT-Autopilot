"""Tests for `mpt validate` and the pre-flight checks `mpt run` reuses.

Covered here:
  - M-57: standalone `python -m mpt_autopilot.validate --config x` works
  - fresh install: only warnings, exit 0 (missing files are the norm there)
  - path mismatch: hard error, exit 1
  - M-50: pipeline reuses this module's checks, not a copy
"""

from __future__ import annotations

from pathlib import Path

from mpt_autopilot import pipeline, validate


def _config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_standalone_parser_accepts_config(tmp_path: Path):
    """M-57: the EPILOG has promised `--config` on the standalone parser
    since the beginning; the flag never existed on it."""
    parser = validate.build_parser()
    args = parser.parse_args(["--config", str(tmp_path / "other.yaml")])
    assert args.config == tmp_path / "other.yaml"


def test_fresh_install_is_only_warnings_and_exit_zero(tmp_path: Path, capsys):
    """Missing seen/jobs files are warns, not errors: on a fresh install they
    legitimately do not exist yet, and the first run creates them."""
    config = _config(tmp_path, "langs:\n  en:\n    file_suffix: ''\n")

    args = validate.build_parser().parse_args(["--config", str(config)])
    code = validate.execute(args)

    assert code == 0
    out = capsys.readouterr().out
    assert "[WARN]" in out
    assert "[ERROR]" not in out


def test_path_mismatch_is_a_hard_error(tmp_path: Path, capsys):
    config = _config(
        tmp_path,
        "langs:\n"
        "  en:\n"
        "    file_suffix: ''\n"
        "batch:\n"
        "  output_dir: ./exports\n"
        "enricher:\n"
        "  videos_dir: ./elsewhere\n",
    )

    args = validate.build_parser().parse_args(["--config", str(config)])
    code = validate.execute(args)

    assert code == 1
    out = capsys.readouterr().out
    assert "[ERROR]" in out
    assert "does not match" in out


def test_matching_paths_pass(tmp_path: Path, capsys):
    config = _config(
        tmp_path,
        "langs:\n"
        "  en:\n"
        "    file_suffix: ''\n"
        "batch:\n"
        "  output_dir: ./exports\n"
        "enricher:\n"
        "  videos_dir: ./exports\n",
    )

    args = validate.build_parser().parse_args(["--config", str(config)])
    code = validate.execute(args)

    assert code == 0
    assert "[ERROR]" not in capsys.readouterr().out


def test_per_language_seen_and_jobs_paths_use_stage_helpers(tmp_path: Path):
    """M-51: the checked paths must be exactly what the stages would open —
    seen.resolve()/jobs.new_path(), not a hand-rolled suffix derivation."""
    from mpt_autopilot import seen
    from mpt_autopilot.pilot import jobs

    config = _config(
        tmp_path,
        "langs:\n  en:\n    file_suffix: ''\n  es:\n    file_suffix: _es\n",
    )
    cfg = validate.shared_config.load(config)
    results = validate._run_checks(cfg)
    warn_details = " ".join(r.detail for r in results if r.status == "warn")

    assert str(seen.resolve(tmp_path / "jobs", "_es")) in warn_details
    assert str(jobs.new_path(tmp_path / "jobs", "_es")) in warn_details


def test_pipeline_reuses_validates_checks():
    """M-50: one implementation — pipeline imports validate's, not a copy."""
    assert pipeline.validate._run_checks is validate._run_checks


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
