"""
cli.py — the single `mpt` entry point.

One command with one subcommand per stage, so a user installs one package and
learns one interface:

    mpt refill     — generate video ideas and refill the jobs queue
    mpt init-seen  — register existing videos so refill won't repeat them
    mpt batch      — render pending jobs through MoneyPrinterTurbo
    mpt enrich     — generate hashtags for rendered videos
    mpt upload     — upload rendered videos to YouTube
    mpt run        — the whole pipeline, in order

`--config` lives at the root (`mpt --config /srv/mpt/config.yaml batch`) and is
also accepted after the subcommand, so muscle memory from the four separate
tools still works.

Each stage's flags are registered by its own `add_arguments()`, so the
subcommand help text and the standalone parsers can never drift apart.

Nothing here imports a stage's settings at module scope: `mpt --help` and every
subcommand's `--help` must work in a directory that has no config.yaml.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NamedTuple

from mpt_autopilot import __version__


# A stage is registered as (name, help text, add-arguments hook, execute hook).
# The hooks are imported lazily inside _register so building the parser never
# pulls in requests/httpx/yaml for stages the user isn't running.
class StageHooks(NamedTuple):
    add_arguments: Callable[[argparse.ArgumentParser], argparse.ArgumentParser]
    execute: Callable[[argparse.Namespace, Path | None], int]
    epilog: str


def _stage_hooks(name: str) -> StageHooks:
    if name == "refill":
        from mpt_autopilot.pilot import refill

        return StageHooks(refill.add_arguments, refill.execute, refill.EPILOG)
    if name == "init-seen":
        from mpt_autopilot.pilot import init_seen

        return StageHooks(init_seen.add_arguments, init_seen.execute, init_seen.EPILOG)
    if name == "batch":
        from mpt_autopilot.batch import run as batch_run

        return StageHooks(batch_run.add_arguments, batch_run.execute, batch_run.EPILOG)
    if name == "enrich":
        from mpt_autopilot.enricher import run as enricher_run

        return StageHooks(enricher_run.add_arguments, enricher_run.execute, enricher_run.EPILOG)
    if name == "upload":
        from mpt_autopilot.uploader import run as uploader_run

        return StageHooks(uploader_run.add_arguments, uploader_run.execute, uploader_run.EPILOG)
    if name == "run":
        from mpt_autopilot import pipeline

        return StageHooks(pipeline.add_arguments, pipeline.execute, pipeline.EPILOG)
    raise ValueError(f"unknown stage '{name}'")


STAGES: tuple[tuple[str, str], ...] = (
    ("refill", "generate video ideas with an LLM and refill the jobs queue"),
    ("init-seen", "register existing .mp4 files so refill won't repeat them"),
    ("batch", "render pending jobs through the MoneyPrinterTurbo API"),
    ("enrich", "generate hashtags for rendered videos with an LLM"),
    ("upload", "upload rendered videos to YouTube"),
    ("run", "run the whole pipeline: refill → batch → enrich → upload"),
)

EPILOG = """
Examples:
  mpt refill --lang en --force
  mpt batch --lang en
  mpt enrich --dir ./exports_en
  mpt upload --account en --dry-run
  mpt run --lang en

  # One config somewhere else entirely
  mpt --config /srv/mpt/config.yaml run --all-langs

Every subcommand has its own --help, e.g. `mpt batch --help`.
"""


def _force_utf8_output() -> None:
    """
    Make stdout/stderr tolerate non-ASCII text.

    Help text, log lines, and Telegram-bound messages all contain arrows, em
    dashes, and non-Latin topic titles. A Windows console defaults to a legacy
    code page (cp1251 here), where writing any of that raises
    UnicodeEncodeError — so `mpt --help` itself would crash. errors="replace"
    is deliberate: garbling one glyph is always better than aborting a batch
    run halfway through.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                # A redirected or already-wrapped stream may refuse; the
                # command itself is still perfectly runnable.
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mpt",
        description="MPT Autopilot — end-to-end automation for MoneyPrinterTurbo.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="Path to config.yaml (default: ./config.yaml). Applies to every subcommand.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"mpt-autopilot {__version__}",
    )

    subparsers = parser.add_subparsers(dest="stage", metavar="<command>")
    for name, help_text in STAGES:
        hooks = _stage_hooks(name)
        sub = subparsers.add_parser(
            name,
            help=help_text,
            description=help_text,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            # Each stage owns its own examples; showing them under
            # `mpt <stage> --help` is the whole reason they exist.
            epilog=hooks.epilog,
        )
        # Accepted after the subcommand too, so `mpt batch --config x.yaml`
        # keeps working for anyone used to the old separate commands. A separate
        # dest is required: a subparser writes its default over the root value,
        # so sharing `config` would silently discard `mpt --config ... batch`.
        sub.add_argument(
            "--config",
            type=Path,
            default=None,
            dest="stage_config",
            metavar="PATH",
            help="Path to config.yaml (default: ./config.yaml)",
        )
        hooks.add_arguments(sub)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _force_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.stage:
        parser.print_help()
        return 1

    # A --config after the subcommand wins over the root one; both are optional.
    config_path: Path | None = getattr(args, "stage_config", None) or args.config

    return _stage_hooks(args.stage).execute(args, config_path)


if __name__ == "__main__":
    sys.exit(main())
