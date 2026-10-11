"""
validate.py — `mpt validate`: pre-flight configuration checks.

Owns the pre-flight checks that `mpt run` also runs before starting any stage
(pipeline.py imports _run_checks from here — one implementation, M-50):
  1. File-access permissions for seen.txt and jobs files (warnings)
  2. Path consistency: batch.output_dir vs enricher.videos_dir (errors)

Exit codes:
    0 — all checks passed (or only warnings)
    1 — at least one hard error found
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

from mpt_autopilot import config as shared_config
from mpt_autopilot import seen
from mpt_autopilot.config import ConfigError
from mpt_autopilot.logger import configure_root
from mpt_autopilot.pilot import jobs

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
    results.extend(_check_audio_normalize(cfg))
    results.extend(_check_prompt_health(cfg))
    return results


# Placeholders the enricher/pilot code actually fills at runtime. A prompt
# referencing anything else renders the literal "{name}" into the LLM call —
# invisible until it degrades the output (R9.3).
_KNOWN_PROMPT_PLACEHOLDERS = frozenset(
    {
        "video_subject",
        "script",
        "platform",
        "language",
        "text",
        "min_tags",
        "max_tags",
        "max_tag_length",
        "excluded_tags",
        "count",
        "videos",
    }
)
_ENRICHER_PROMPT_KEYS = (
    "prompt_detect_language",
    "prompt_generate",
    "prompt_detect_and_generate",
    "prompt_batch",
    "prompt_title_description",
)


def _check_prompt_health(cfg: shared_config.Config) -> list[CheckResult]:
    """
    R9.2/R9.3: the prompts are production content living in config.yaml —
    check what a typo there would silently break, at validation time:

    1. a hashtag example longer than max_tag_length teaches the model a tag
       its own filter will discard (B-11's exact mechanism);
    2. a placeholder the code never fills ({tag_count} instead of
       {max_tags}) ships the literal braces to the LLM.
    """
    results: list[CheckResult] = []
    try:
        enricher_sec = cfg.section("enricher")
    except ConfigError:
        return results

    max_tag_length = int(enricher_sec.get("max_tag_length", 20))

    for key in _ENRICHER_PROMPT_KEYS:
        prompt = enricher_sec.get(key)
        if not isinstance(prompt, str) or not prompt:
            continue
        # {{...}} doubles are literal braces in .format() — only single
        # braces are placeholders.
        placeholders = set(re.findall(r"(?<!\{)\{([a-z_]+)\}(?!\})", prompt))
        unknown = sorted(placeholders - _KNOWN_PROMPT_PLACEHOLDERS)
        if unknown:
            results.append(
                CheckResult(
                    f"enricher.{key}",
                    f"unknown placeholder(s) {unknown} — the code never fills "
                    f"these; known: {sorted(_KNOWN_PROMPT_PLACEHOLDERS)}",
                    "warn",
                )
            )
        long_tags = sorted({t for t in re.findall(r"#(\w+)", prompt) if len(t) > max_tag_length})
        if long_tags:
            results.append(
                CheckResult(
                    f"enricher.{key}",
                    f"example hashtag(s) longer than max_tag_length="
                    f"{max_tag_length} and will be discarded by the filter: "
                    f"{['#' + t for t in long_tags]}",
                    "warn",
                )
            )
    return results


def _check_audio_normalize(cfg: shared_config.Config) -> list[CheckResult]:
    """
    R5: `batch.audio_normalize: true` promises an ffmpeg pass on every
    finished video. When ffmpeg is missing from PATH the pass is silently
    skipped at render time — surface that here, where the user is looking at
    their config, instead.
    """
    try:
        batch_sec = cfg.section("batch")
    except ConfigError:
        return []
    if not batch_sec.get("audio_normalize"):
        return []
    if shutil.which("ffmpeg") is None:
        return [
            CheckResult(
                "batch.audio_normalize",
                "enabled but ffmpeg is not on PATH — the audio pass will be skipped",
                "warn",
            )
        ]
    return []


def _check_file_access(cfg: shared_config.Config) -> list[CheckResult]:
    """Verify that every seen.txt and jobs file (per language) is readable."""
    results: list[CheckResult] = []
    langs = cfg.langs()

    try:
        batch_sec = cfg.section("batch")
    except ConfigError:
        batch_sec = {}

    seen_dir_str = cfg.paths().get("seen_dir") or "./jobs"
    seen_dir = cfg.resolve(str(seen_dir_str))
    # seen.resolve() is the same helper the stages use, so the checked path
    # can never drift from the path a stage would actually open (M-51).
    seen_base = seen.resolve(seen_dir, "")

    seen_checked: set[Path] = set()

    # Check base seen file
    result = _check_readable(seen_base, "seen")
    results.append(result)
    seen_checked.add(seen_base)

    # Per-language seen files
    for lang_code in langs:
        file_suffix = langs[lang_code].get("file_suffix", "")
        lang_seen = seen.resolve(seen_dir, file_suffix)
        if lang_seen in seen_checked:
            continue
        seen_checked.add(lang_seen)
        label = f"seen ({lang_code})" if file_suffix else "seen"
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
        # jobs.new_path() — the same helper pilot/refill uses to write the file
        # in the first place (M-51).
        jobs_file = jobs.new_path(jobs_dir, file_suffix)
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
    except ConfigError:
        batch_sec = {}

    exports_base_str = batch_sec.get("output_dir") or (
        cfg.paths().get("exports_dir") or "./exports"
    )
    exports_base = cfg.resolve(str(exports_base_str))

    try:
        enricher_sec = cfg.section("enricher")
    except ConfigError:
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
        # cfg.suffix_dir() — the same derivation exports_dir_for() uses on the
        # real path, so the check compares against what will actually happen
        # (M-51).
        expected_dir = cfg.suffix_dir(exports_base, lang_code)
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
    """
    Return a CheckResult for a file path.

    A missing file is a warn, deliberately: on a fresh install seen.txt and
    jobs.yaml legitimately do not exist yet — the first run creates them, so
    failing hard here would make `mpt validate` useless out of the box.
    """
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


def _print_results(results: list[CheckResult], title: str = "[validate]") -> None:
    """Print all check results in a consolidated block.

    `title` lets the pipeline reuse this printer for its own pre-flight block
    without wearing the validate branding.
    """
    if not results:
        print(f"{title} No checks to run.")
        return

    print(f"\n{'-' * 60}")
    print(f"{title} {len(results)} check(s):")

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
    # --config lives here, NOT in add_arguments(): the root `mpt` parser adds
    # its own --config on top of add_arguments() (with dest="stage_config"),
    # and a second same-named option here would clash with it. Standalone
    # `python -m mpt_autopilot.validate --config x` needs the flag — the EPILOG
    # above has promised exactly that example since the beginning (M-57).
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="Config file path (default: config.yaml)",
    )
    return add_arguments(parser)


def main() -> None:
    # Give the root logger a handler before any stage runs (M-1).
    configure_root()
    sys.exit(execute(build_parser().parse_args()))


if __name__ == "__main__":
    main()
