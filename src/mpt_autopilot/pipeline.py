"""
pipeline.py — `mpt run`, the whole chain in one command.

Runs the four stages in the only order that makes sense, once per language:

    refill → batch → enrich → upload

Each stage is called in-process through its own `execute()`, so this adds no
subprocess overhead and every stage keeps its existing behaviour, logging, and
exit codes.

Language isolation: a language that fails does not abort the ones after it when
`--continue-on-error` is set; without that flag the pipeline stops at the first
failing stage, which is the safer default for an unattended cron run.

Exit codes aggregate across everything that ran, with failure dominating a
quota stop:

    1 — at least one stage failed
    2 — no failures, but at least one uploader stage stopped on quota
    0 — everything succeeded (or was skipped)

Stage-to-stage wiring:
  * `batch` renders into `batch.output_dir` (suffixed per language, e.g.
    `exports_es/`), which is exactly where `enrich` then looks.
  * `upload` needs an account name; by default the language code is used, and
    `pipeline.accounts:` overrides that per language when the channel is not
    named after the language.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from mpt_autopilot import config as shared_config
from mpt_autopilot.config import ConfigError

STAGES: tuple[str, ...] = ("refill", "batch", "enrich", "upload")

# Exit codes, matching the uploader's contract.
EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_QUOTA_STOP = 2


@dataclass
class StageResult:
    lang: str
    stage: str
    code: int
    note: str = ""


def add_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Register `mpt run` flags on `parser`."""
    langs = parser.add_mutually_exclusive_group()
    langs.add_argument(
        "--lang",
        action="append",
        dest="langs",
        default=None,
        metavar="CODE",
        help="Language to process. Repeatable. Defaults to every language in config.yaml.",
    )
    langs.add_argument(
        "--all-langs",
        action="store_true",
        help="Process every language defined in config.yaml's langs: section.",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        choices=STAGES,
        metavar="STAGE",
        help=f"Run only these stages. Repeatable. One of: {', '.join(STAGES)}.",
    )
    parser.add_argument(
        "--skip",
        action="append",
        default=None,
        choices=STAGES,
        metavar="STAGE",
        help=f"Skip these stages. Repeatable. One of: {', '.join(STAGES)}.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Pass --dry-run to every stage that supports it. refill is skipped "
            "entirely, because generating ideas has no dry equivalent."
        ),
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Keep going after a stage fails instead of stopping this language.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Passed to the upload stage: only process the first N videos per account.",
    )
    return parser


EPILOG = """
Examples:
  mpt run                              # every language in config.yaml
  mpt run --lang en                    # one language
  mpt run --lang en --lang es          # two, in that order
  mpt run --dry-run                    # show what would happen, touch nothing
  mpt run --only batch --only enrich   # a slice of the pipeline
  mpt run --skip upload                # produce everything, upload later
  mpt run --continue-on-error          # don't stop a language on the first failure

  # From cron, with a config elsewhere
  mpt --config /srv/mpt/config.yaml run

Exit codes: 0 = ok, 1 = a stage failed, 2 = an upload stopped on YouTube's
            daily quota (failure wins over a quota stop).

See docs/pipeline.md for stage wiring and failure handling.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mpt run",
        description="Run the whole pipeline: refill → batch → enrich → upload, per language.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="Path to config.yaml (default: ./config.yaml)",
    )
    return add_arguments(parser)


def resolve_stages(only: list[str] | None, skip: list[str] | None) -> list[str]:
    selected = list(only) if only else list(STAGES)
    if skip:
        selected = [s for s in selected if s not in set(skip)]
    # Preserve pipeline order regardless of the order flags were given in.
    return [s for s in STAGES if s in set(selected)]


def resolve_langs(cfg: shared_config.Config, requested: list[str] | None) -> list[str]:
    configured = list(cfg.langs())
    if not requested:
        return configured
    unknown = [code for code in requested if code not in configured]
    if unknown:
        raise ConfigError(
            f"unknown language(s) {unknown} — config.yaml langs: defines {configured or '(none)'}"
        )
    # Deduplicate while keeping the order the user asked for.
    return list(dict.fromkeys(requested))


def account_for(cfg: shared_config.Config, lang: str) -> str:
    """
    The uploader account for a language: `pipeline.accounts.<lang>` when set,
    otherwise the language code itself.
    """
    mapping = cfg.section("pipeline").get("accounts") or {}
    if not isinstance(mapping, dict):
        raise ConfigError("config.yaml: pipeline.accounts must be a mapping of lang → account")
    return str(mapping.get(lang, lang))


def exports_dir_for(cfg: shared_config.Config, lang: str) -> Path:
    """
    Where `batch` writes this language's videos, and therefore where `enrich`
    reads them. Delegates to Config.suffix_dir() for a single source of truth.
    """
    batch_sec = cfg.section("batch")
    base_str = batch_sec.get("output_dir") or (cfg.paths().get("exports_dir") or "./exports")
    base = cfg.resolve(str(base_str))
    return cfg.suffix_dir(base, lang)


# ── Stage invocations ─────────────────────────────────────────────────────────


def _run_refill(lang: str, config_path: Path | None, args: argparse.Namespace) -> int:
    from mpt_autopilot.pilot import refill

    stage_args = argparse.Namespace(
        lang=lang,
        jobs_dir=None,
        seen_dir=None,
        force=False,
        count=None,
        threshold=None,
        topic=None,
        topics_file=None,
        theme=None,
    )
    return refill.execute(stage_args, config_path)


def _run_batch(lang: str, config_path: Path | None, args: argparse.Namespace) -> int:
    from mpt_autopilot.batch import run as batch_run

    stage_args = argparse.Namespace(
        jobs=None,
        dry_run=args.dry_run,
        status=False,
        list_voices=None,
        list_bgm=None,
        upload_bgm=None,
        seen=None,
        lang=lang,
    )
    return batch_run.execute(stage_args, config_path)


def _run_enrich(
    lang: str, config_path: Path | None, args: argparse.Namespace, videos_dir: Path
) -> int:
    from mpt_autopilot.enricher import run as enricher_run

    stage_args = argparse.Namespace(
        dir=videos_dir,
        file=None,
        lang=None,  # let the enricher detect per file; config langs are codes, not LLM names
        platform=None,
        force=False,
        dry_run=args.dry_run,
    )
    return enricher_run.execute(stage_args, config_path)


def _run_upload(lang: str, config_path: Path | None, args: argparse.Namespace, account: str) -> int:
    from mpt_autopilot.uploader import run as uploader_run

    stage_args = argparse.Namespace(
        account=account,
        all_accounts=False,
        dry_run=args.dry_run,
        limit=args.limit,
        accounts_file=None,
    )
    return uploader_run.execute(stage_args, config_path)


# ── Orchestration ─────────────────────────────────────────────────────────────


def _aggregate(results: list[StageResult]) -> int:
    codes = {r.code for r in results}
    if EXIT_FAILURES in codes or any(c not in (EXIT_OK, EXIT_QUOTA_STOP) for c in codes):
        return EXIT_FAILURES
    if EXIT_QUOTA_STOP in codes:
        return EXIT_QUOTA_STOP
    return EXIT_OK


def execute(args: argparse.Namespace, config_path: Path | None = None) -> int:
    if config_path is None:
        config_path = getattr(args, "config", None)

    try:
        cfg = shared_config.load(config_path)
        langs = resolve_langs(cfg, args.langs)
    except (ConfigError, FileNotFoundError) as exc:
        print(f"[ERROR] {exc}")
        return EXIT_FAILURES

    if not langs:
        print(
            "[ERROR] no languages to run — add a langs: section to config.yaml "
            "(see config.example.yaml)"
        )
        return EXIT_FAILURES

    stages = resolve_stages(args.only, args.skip)
    if not stages:
        print("[ERROR] --only/--skip left no stages to run")
        return EXIT_FAILURES

    if args.dry_run and "refill" in stages:
        # Generating ideas has no dry equivalent, and running it for real during
        # a --dry-run would silently mutate the jobs queue.
        stages = [s for s in stages if s != "refill"]
        print("[run] --dry-run: skipping refill (no dry-run mode for idea generation)")

    print(f"[run] languages: {', '.join(langs)}")
    print(f"[run] stages   : {' → '.join(stages) or '(none)'}")
    if args.dry_run:
        print("[run] dry-run  : no videos rendered, no LLM calls, nothing uploaded")

    results: list[StageResult] = []

    for lang in langs:
        print(f"\n{'=' * 60}\n[run] language: {lang}\n{'=' * 60}")
        for stage in stages:
            print(f"\n[run] {lang} → {stage}")
            try:
                if stage == "refill":
                    code = _run_refill(lang, config_path, args)
                elif stage == "batch":
                    code = _run_batch(lang, config_path, args)
                elif stage == "enrich":
                    videos_dir = exports_dir_for(cfg, lang)
                    if not videos_dir.is_dir():
                        print(f"[run] {lang} → enrich: {videos_dir} does not exist yet — skipping")
                        results.append(
                            StageResult(lang, stage, EXIT_OK, "skipped (no exports dir)")
                        )
                        continue
                    code = _run_enrich(lang, config_path, args, videos_dir)
                else:
                    code = _run_upload(lang, config_path, args, account_for(cfg, lang))
            except (ConfigError, FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
                # A stage-reported failure must not kill the other languages, but
                # programming errors (AssertionError, TypeError, …) should still
                # surface instead of being silently swallowed here.
                print(f"[run] {lang} → {stage} crashed: {exc}")
                code = EXIT_FAILURES

            results.append(StageResult(lang, stage, code))

            if code != EXIT_OK and not args.continue_on_error:
                print(
                    f"[run] {lang} → {stage} returned {code} — stopping this language "
                    f"(use --continue-on-error to keep going)"
                )
                break

    print(f"\n{'=' * 60}\n[run] summary\n{'=' * 60}")
    labels = {EXIT_OK: "ok", EXIT_FAILURES: "failed", EXIT_QUOTA_STOP: "quota-stop"}
    for r in results:
        label = labels.get(r.code, str(r.code))
        note = f"  ({r.note})" if r.note else ""
        print(f"  {r.lang:<6} {r.stage:<10} {label}{note}")

    return _aggregate(results)


def main() -> None:
    sys.exit(execute(build_parser().parse_args()))


if __name__ == "__main__":
    main()
