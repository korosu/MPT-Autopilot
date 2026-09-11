"""
validate.py — `mpt validate`: pre-flight configuration checks.

Runs the same checks that `mpt run --dry-run` does before starting stages:
  1. File-access permissions for seen.txt and jobs files (warnings)
  2. Path consistency: batch.output_dir vs enricher.videos_dir (errors)

Exit codes:
    0 — all checks passed (or only warnings)
    1 — at least one hard error found
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mpt_autopilot import config as shared_config
from mpt_autopilot.config import ConfigError


def add_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Register `mpt validate` flags on `parser`."""
    # Note: --config is added by the root CLI parser, not here.
    return parser


EPILOG = """
Examples:
  mpt validate                         # validate current config.yaml
  mpt validate --config /path/config.yaml  # validate specific config

Exit codes: 0 = ok (warnings allowed), 1 = hard error found
"""


def _check_file_access(cfg: shared_config.Config) -> list[tuple[str, str, bool]]:
    """
    Verify that every seen.txt and jobs file (per language) is readable.

    These are soft warnings — files may legitimately not exist yet in a fresh
    setup, but the user should know about them.
    """
    issues: list[tuple[str, str, bool]] = []
    langs = cfg.langs()

    try:
        batch_sec = cfg.section("batch")
    except (ConfigError, ValueError):
        batch_sec = {}

    # Resolve seen file
    seen_val = batch_sec.get("seen_file")
    if seen_val is not None:
        seen_base = cfg.resolve(str(seen_val))
    else:
        seen_dir_str = batch_sec.get("seen_dir") or cfg.paths().get("seen_dir") or "./jobs"
        seen_dir = cfg.resolve(str(seen_dir_str))
        seen_base = seen_dir / "seen.txt"

    # Check base seen file
    _check_readable(seen_base, "seen", issues)

    # Per-language seen files
    for lang_code in langs:
        file_suffix = langs[lang_code].get("file_suffix", "")
        slug = file_suffix.lstrip("_")
        lang_seen = seen_base.parent / f"seen_{slug}.txt" if slug else seen_base
        _check_readable(lang_seen, "seen", issues, lang_code)

    # Jobs files
    jobs_dir_str = batch_sec.get("jobs_dir") or cfg.paths().get("jobs_dir") or "./jobs"
    jobs_dir = cfg.resolve(str(jobs_dir_str))

    # Direct jobs path if configured
    cfg_jobs = batch_sec.get("jobs")
    if cfg_jobs:
        direct_jobs = cfg.resolve(str(cfg_jobs))
        _check_readable(direct_jobs, "jobs", issues)

    for lang_code in langs:
        file_suffix = langs[lang_code].get("file_suffix", "")
        jobs_file = jobs_dir / f"jobs{file_suffix}.yaml"
        _check_readable(jobs_file, "jobs", issues, lang_code)

    return issues


def _check_path_consistency(cfg: shared_config.Config) -> list[tuple[str, str, bool]]:
    """
    Verify that batch.output_dir (with language suffix) matches the directory
    enricher scans for video files (enricher.videos_dir).
    """
    issues: list[tuple[str, str, bool]] = []

    try:
        batch_sec = cfg.section("batch")
    except (ConfigError, ValueError):
        batch_sec = {}

    exports_base_str = batch_sec.get("output_dir") or (
        cfg.paths().get("exports_dir") or "./exports"
    )
    exports_base = cfg.resolve(str(exports_base_str))

    try:
        enricher_sec = cfg.section("enricher")
    except (ConfigError, ValueError):
        enricher_sec = {}

    enrich_videos_raw = enricher_sec.get("videos_dir")
    if enrich_videos_raw is None:
        # No explicit enricher.videos_dir — pipeline passes exports_dir_for()
        # explicitly, so the paths will match at runtime. No issue.
        return issues

    enrich_dir = cfg.resolve(str(enrich_videos_raw))
    langs = cfg.langs()

    for lang_code in langs:
        file_suffix = langs[lang_code].get("file_suffix", "")
        expected_dir = (
            exports_base.parent / f"{exports_base.name}{file_suffix}"
            if file_suffix
            else exports_base
        )
        if enrich_dir == expected_dir:
            continue
        issues.append(
            (
                "path-consistency",
                f"{lang_code}: enricher.videos_dir={enrich_dir} does not match "
                f"batch output dir {expected_dir} (exports_dir{file_suffix or '(no suffix)'}) — "
                f"enrich may scan the wrong directory",
                True,  # hard error
            )
        )

    return issues


def _check_readable(
    path: Path,
    file_type: str,
    issues: list[tuple[str, str, bool]],
    lang: str = "",
) -> None:
    """Append a soft warning if path is missing or not readable."""
    import os

    label = f"{lang}: " if lang else ""
    if not path.exists():
        issues.append((file_type, f"{label}{file_type} file not found: {path}", False))
    elif not os.access(path, os.R_OK):
        issues.append(
            (file_type, f"{label}{file_type} file exists but not readable: {path}", False)
        )


def _print_issues(issues: list[tuple[str, str, bool]]) -> int:
    """Print all collected issues and return number of hard errors."""
    if not issues:
        print("[validate] All checks passed.")
        return 0

    hard = [i for i in issues if i[2]]
    soft = [i for i in issues if not i[2]]

    if hard:
        print(f"\n{'-' * 60}")
        print(f"[validate] {len(hard)} error(s):")
        for stage, msg, _ in hard:
            print(f"  [ERROR] [{stage}] {msg}")
        print(f"{'-' * 60}")

    if soft:
        print(f"\n{'-' * 60}")
        print(f"[validate] {len(soft)} warning(s):")
        for stage, msg, _ in soft:
            print(f"  [WARN]  [{stage}] {msg}")
        print(f"{'-' * 60}")

    return len(hard)


def execute(args: argparse.Namespace, config_path: Path | None = None) -> int:
    """Run validation from already-parsed arguments."""
    if config_path is None:
        config_path = getattr(args, "config", None)

    try:
        cfg = shared_config.load(config_path)
    except (ConfigError, FileNotFoundError) as exc:
        print(f"[ERROR] {exc}")
        return 1

    issues: list[tuple[str, str, bool]] = []
    issues.extend(_check_file_access(cfg))
    issues.extend(_check_path_consistency(cfg))

    error_count = _print_issues(issues)
    return 1 if error_count > 0 else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mpt validate",
        description="Pre-flight configuration checks for MPT Autopilot.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    return add_arguments(parser)


def main() -> None:
    sys.exit(execute(build_parser().parse_args()))


if __name__ == "__main__":
    main()
