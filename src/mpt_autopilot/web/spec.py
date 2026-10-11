"""
web/spec.py — argparse introspection, the web UI's only source of flag truth.

Every control on the page is generated from the real parsers: the module walks
`cli.build_parser()`, which already builds one subparser per stage through that
stage's own `add_arguments()`. Nothing is executed and no settings are loaded,
so this stays safe to call in a directory with no config.yaml.

A hand-written flag table would be a second definition to keep in sync, and it
would silently drift the first time a stage grows a flag — which is exactly the
drift `add_arguments()` exists to prevent.

argparse exposes no public way to tell `store` from `append` (both are plain
Actions with the same attributes), so actions are classified by class name via
_ACTION_BEHAVIOURS. An unknown name raises instead of guessing; tests/web/
test_spec.py walks every real action, so a rename in a future CPython fails in
CI rather than in a user's browser.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from mpt_autopilot import cli

FlagKind = Literal["bool", "text", "int", "path", "choice", "lang", "optional_value"]

# type(action).__name__ → how the action consumes values.
_ACTION_BEHAVIOURS: dict[str, str] = {
    "_StoreAction": "store",
    "_AppendAction": "append",
    "_ExtendAction": "append",
    "_StoreTrueAction": "flag",
    "_StoreFalseAction": "flag",
    "_StoreConstAction": "flag",
    "_CountAction": "flag",
    "_HelpAction": "skip",
    "_VersionAction": "skip",
    "_SubParsersAction": "skip",
}

# `stage_config` is cli.py's post-subcommand --config; the web passes the root
# --config itself, so offering a second one would only create a way to run a
# stage against a different config than the page was built from.
_SKIP_DESTS = frozenset({"stage_config", "help", "version"})

# Flags shown up front; everything else goes under "Advanced". Presentation
# only — the spec itself still carries every flag.
COMMON_DESTS = frozenset({"lang", "langs", "all_langs", "dry_run", "force"})

# Stages whose `--lang` means "a key in config.yaml's langs:" and can therefore
# be a picker. `mpt enrich --lang` uses metavar LANGUAGE for a free-text LLM
# language name ("English", "ru"), so it stays a text field.
_LANG_METAVARS = frozenset({"LANG", "CODE"})


@dataclass(frozen=True)
class FlagSpec:
    dest: str
    option: str
    kind: FlagKind
    multi: bool
    required: bool
    choices: tuple[str, ...] | None
    default: str | None
    help: str
    advanced: bool


@dataclass(frozen=True)
class StageSpec:
    name: str
    help: str
    flags: tuple[FlagSpec, ...]
    exclusive: tuple[tuple[str, ...], ...]
    required_exclusive: tuple[tuple[str, ...], ...]

    def flag(self, dest: str) -> FlagSpec | None:
        for spec in self.flags:
            if spec.dest == dest:
                return spec
        return None


def behaviour_of(action: argparse.Action) -> str:
    name = type(action).__name__
    try:
        return _ACTION_BEHAVIOURS[name]
    except KeyError:
        raise ValueError(
            f"argparse action {name} (--{action.dest}) is not classified — "
            f"add it to spec._ACTION_BEHAVIOURS"
        ) from None


def _option(action: argparse.Action) -> str:
    """The flag as a user types it: the first long form, else the first form."""
    for option in action.option_strings:
        if option.startswith("--"):
            return option
    return action.option_strings[0]


def _kind(action: argparse.Action) -> FlagKind:
    if action.nargs == 0:
        return "bool"
    if action.choices:
        return "choice"
    if action.nargs == "?":
        # nargs="?" carries three states (absent / bare flag / flag with a
        # value), so it cannot collapse into a checkbox or a text field alone.
        return "optional_value"
    if isinstance(action.metavar, str) and action.metavar in _LANG_METAVARS:
        return "lang"
    if action.type is Path:
        return "path"
    if action.type is int:
        return "int"
    return "text"


def _default(action: argparse.Action) -> str | None:
    """A displayable default, or None when there is nothing worth showing."""
    default = action.default
    if default is None or default is False or default == [] or default == "":
        return None
    return str(default)


def _help(action: argparse.Action) -> str:
    # argparse help strings are assembled from adjacent literals and wrap over
    # several lines; HTML wants one line.
    return " ".join((action.help or "").split())


def _groups(
    parser: argparse.ArgumentParser,
) -> tuple[tuple[tuple[str, ...], ...], tuple[tuple[str, ...], ...]]:
    """(exclusive, required_exclusive) as tuples of dests."""
    plain: list[tuple[str, ...]] = []
    required: list[tuple[str, ...]] = []
    for group in parser._mutually_exclusive_groups:
        dests = tuple(a.dest for a in group._group_actions if a.dest not in _SKIP_DESTS)
        if not dests:
            continue
        if group.required:
            required.append(dests)
        else:
            plain.append(dests)
    return tuple(plain), tuple(required)


def _stage_spec(name: str, parser: argparse.ArgumentParser) -> StageSpec:
    exclusive, required_exclusive = _groups(parser)
    in_required_group = {dest for group in required_exclusive for dest in group}

    flags: list[FlagSpec] = []
    for action in parser._actions:
        if action.dest in _SKIP_DESTS:
            continue
        behaviour = behaviour_of(action)
        if behaviour == "skip":
            continue
        if not action.option_strings:
            raise ValueError(
                f"mpt {name} has a positional argument ({action.dest}); "
                f"the web UI only renders options"
            )
        flags.append(
            FlagSpec(
                dest=action.dest,
                option=_option(action),
                kind=_kind(action),
                multi=behaviour == "append",
                required=bool(action.required),
                choices=tuple(str(c) for c in action.choices) if action.choices else None,
                default=_default(action),
                help=_help(action),
                advanced=not (
                    action.required
                    or action.dest in COMMON_DESTS
                    or action.dest in in_required_group
                ),
            )
        )

    return StageSpec(
        name=name,
        help=" ".join((parser.description or "").split()),
        flags=tuple(flags),
        exclusive=exclusive,
        required_exclusive=required_exclusive,
    )


def _stage_parsers(root: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in root._actions:
        choices = action.choices
        if action.dest != "stage" or not isinstance(choices, dict):
            continue
        return {
            str(name): parser
            for name, parser in choices.items()
            if isinstance(parser, argparse.ArgumentParser)
        }
    raise ValueError("the mpt root parser has no subcommands — cli.build_parser() changed")


def stage_specs() -> tuple[StageSpec, ...]:
    """Introspect every registered `mpt` subcommand, in cli.STAGES order."""
    parsers = _stage_parsers(cli.build_parser())
    return tuple(_stage_spec(name, parser) for name, parser in parsers.items())


def as_dict(
    specs: Sequence[StageSpec],
    langs: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    """The JSON the page renders its controls from."""
    return {
        "langs": [
            {"code": code, "label": str(entry.get("label", code))} for code, entry in langs.items()
        ],
        "stages": [
            {
                "name": spec.name,
                "help": spec.help,
                "flags": [
                    {
                        "dest": flag.dest,
                        "option": flag.option,
                        "kind": flag.kind,
                        "multi": flag.multi,
                        "required": flag.required,
                        "choices": list(flag.choices) if flag.choices else None,
                        "default": flag.default,
                        "help": flag.help,
                        "advanced": flag.advanced,
                    }
                    for flag in spec.flags
                ],
                "exclusive": [list(group) for group in spec.exclusive],
                "required_exclusive": [list(group) for group in spec.required_exclusive],
            }
            for spec in specs
        ],
    }
