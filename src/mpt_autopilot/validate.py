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
import os
import sys
from pathlib import Path

from mpt_autopilot import config as shared_config
from mpt_autopilot.config import ConfigError

# ── Data types ───────────────────────────────────────────────────────────────


class CheckResult:
    """Result of one validation check."""

    __slots__ = ("check", "detail", "status")

    def __init__(self, check: str, detail: str, status: str) -> None:
        self.check = check  # e.g. "seen", "jobs", "path-consistency"
        self.detail = detail  # human-readable description with path
        self.status = status  # "ok", "warn", "error"


# ── Public API ───────────────────────────────────────────────────────────────


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


def execute(args: argparse.Namespace, config_path: Path | None = None) -> int:
    """Run validation from already-parsed arguments."""
    if config_path is None:
        config_path = getattr(args, "config", None)

    try:
        cfg = shared_config.load(config_path)
    except (ConfigError, FileNotFoundError) as exc:
        print(f"[ERROR] {exc}")
        return 1

    results = _run_checks(cfg)
    _print_results(results)
    return 1 if any(r.status == "error" for r in results) else 0


# ── Checks ───────────────────────────────────────────────────────────────────


def _run_checks(cfg: shared_config.Config) -> list[CheckResult]:
    """Run all validation checks and return results."""
    results: list[CheckResult] = []
    results.extend(_check_file_access(cfg))
    results.extend(_check_path_consistency(cfg))
    return results


def _check_file_access(cfg: shared_config.Config) -> list[CheckResult]:
    """Verify that every seen.txt and jobs file (per language) is readable."""
    results: list[CheckResult] = []
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

    seen_checked: set[Path] = set()

    # Check base seen file
    result = _check_readable(seen_base, "seen")
    results.append(result)
    seen_checked.add(seen_base)

    # Per-language seen files
    for lang_code in langs:
        file_suffix = langs[lang_code].get("file_suffix", "")
        slug = file_suffix.lstrip("_")
        lang_seen = seen_base.parent / f"seen_{slug}.txt" if slug else seen_base
        if lang_seen in seen_checked:
            continue
        seen_checked.add(lang_seen)
        label = f"seen ({lang_code})" if slug else "seen"
        results.append(_check_readable(lang_seen, label))

    # Jobs files
    jobs_dir_str = batch_sec.get("jobs_dir") or cfg.paths().get("jobs_dir") or "./jobs"
    jobs_dir = cfg.resolve(str(jobs_dir_str))

    jobs_checked: set[Path] = set()

    # Direct jobs path if configured
    cfg_jobs = batch_sec.get("jobs")
    if cfg_jobs:
        direct_jobs = cfg.resolve(str(cfg_jobs))
        results.append(_check_readable(direct_jobs, "jobs"))
        jobs_checked.add(direct_jobs)

    for lang_code in langs:
        file_suffix = langs[lang_code].get("file_suffix", "")
        jobs_file = jobs_dir / f"jobs{file_suffix}.yaml"
        if jobs_file in jobs_checked:
            continue
        jobs_checked.add(jobs_file)
        label = f"jobs ({lang_code})" if file_suffix else "jobs"
        results.append(_check_readable(jobs_file, label))

    return results


def _check_path_consistency(cfg: shared_config.Config) -> list[CheckResult]:
    """Verify that batch.output_dir matches enricher.videos_dir."""
    results: list[CheckResult] = []

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
        results.append(
            CheckResult(
                "enricher.videos_dir",
                f"not set; pipeline passes {exports_base} explicitly [OK]",
                "ok",
            )
        )
        return results

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
            results.append(
                CheckResult(
                    f"enricher.videos_dir ({lang_code})",
                    f"{enrich_dir} matches batch {expected_dir.name} [OK]",
                    "ok",
                )
            )
        else:
            results.append(
                CheckResult(
                    f"enricher.videos_dir ({lang_code})",
                    f"{enrich_dir} does not match batch {expected_dir} [ERROR]",
                    "error",
                )
            )

    return results


# ── Helpers ──────────────────────────────────────────────────────────────────


def _check_readable(path: Path, label: str) -> CheckResult:
    """Return a CheckResult for a file path."""
    if not path.exists():
        return CheckResult(
            label,
            f"{path} — file not found",
            "warn",
        )
    if not os.access(path, os.R_OK):
        return CheckResult(
            label,
            f"{path} — exists but not readable",
            "warn",
        )
    return CheckResult(label, str(path), "ok")


def _print_results(results: list[CheckResult]) -> None:
    """Print all check results in a consolidated block."""
    if not results:
        print("[validate] No checks to run.")
        return

    print(f"\n{'-' * 60}")
    print(f"[validate] {len(results)} check(s):")

    for r in results:
        tag = {"ok": "[OK]", "warn": "[WARN]", "error": "[ERROR]"}.get(r.status, r.status)
        print(f"  {tag} [{r.check}] {r.detail}")

    print(f"{'-' * 60}")


# ── Entry point ──────────────────────────────────────────────────────────────


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
