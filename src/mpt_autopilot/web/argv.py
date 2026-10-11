"""
web/argv.py — turn a submitted form into an argument list.

The single place where browser input becomes a command line, so it is the
single place that has to be careful. Two rules do the work:

  * The result is always a `list[str]` handed straight to Popen with the default
    shell=False. A value like `en; rm -rf /` is one list element and stays one
    opaque string for the child — there is no shell to interpret it.
  * Nothing is accepted that the spec does not describe. An unknown dest, a
    non-numeric number, a value outside `choices`, a missing required flag, or
    two members of one mutually exclusive group all raise FormError before a
    process is created, so the user gets a 400 instead of a stage crash.

`mpt` is invoked as `python -m mpt_autopilot.cli` rather than the `mpt` console
script: it needs no PATH entry and picks up the same interpreter the server runs
under, which is what makes it work unchanged under `uv run`.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path

from mpt_autopilot.web.spec import FlagSpec, StageSpec


class FormError(ValueError):
    """Submitted values don't match the stage's parser. Never a server fault."""


def _scalar(flag: FlagSpec, value: object) -> str | None:
    """The string to pass after the option, or None when the flag stays unset."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise FormError(f"{flag.option} takes a value, not a checkbox state")
    text = str(value).strip()
    if not text:
        return None
    if flag.kind == "int":
        try:
            return str(int(text))
        except ValueError:
            raise FormError(f"{flag.option} needs a whole number — got {text!r}") from None
    if flag.choices is not None and text not in flag.choices:
        raise FormError(f"{flag.option} must be one of {', '.join(flag.choices)} — got {text!r}")
    return text


def _render(flag: FlagSpec, value: object) -> list[str]:
    if flag.kind == "bool":
        if value is None or value is False:
            return []
        if value is not True:
            raise FormError(f"{flag.option} is a switch — send true or false")
        return [flag.option]

    if flag.kind == "optional_value":
        # nargs="?": absent, bare flag (argparse uses `const`), or flag + value.
        if value is None or value is False:
            return []
        if value is True:
            return [flag.option]
        text = str(value).strip()
        return [flag.option, text] if text else []

    if flag.multi:
        items = value if isinstance(value, list) else [value]
        argv: list[str] = []
        for item in items:
            text = _scalar(flag, item)
            if text is not None:
                argv += [flag.option, text]
        return argv

    text = _scalar(flag, value)
    return [flag.option, text] if text is not None else []


def _label(stage: StageSpec, dest: str) -> str:
    flag = stage.flag(dest)
    return flag.option if flag is not None else f"--{dest}"


def _check_groups(stage: StageSpec, given: set[str]) -> None:
    for flag in stage.flags:
        if flag.required and flag.dest not in given:
            raise FormError(f"mpt {stage.name} needs {flag.option}")

    for group in stage.required_exclusive:
        chosen = [dest for dest in group if dest in given]
        options = " or ".join(_label(stage, dest) for dest in group)
        if not chosen:
            raise FormError(f"mpt {stage.name} needs {options}")
        if len(chosen) > 1:
            raise FormError(f"mpt {stage.name}: pass only one of {options}")

    for group in stage.exclusive:
        chosen = [dest for dest in group if dest in given]
        if len(chosen) > 1:
            options = " and ".join(_label(stage, dest) for dest in chosen)
            raise FormError(f"mpt {stage.name}: {options} cannot be combined")


def flag_argv(stage: StageSpec, form: Mapping[str, object]) -> list[str]:
    """Just the flags, in the order the stage's parser declares them."""
    by_dest = {flag.dest: flag for flag in stage.flags}
    unknown = sorted(set(form) - set(by_dest))
    if unknown:
        raise FormError(f"mpt {stage.name} has no flag {', '.join(unknown)}")

    argv: list[str] = []
    given: set[str] = set()
    for flag in stage.flags:
        if flag.dest not in form:
            continue
        rendered = _render(flag, form[flag.dest])
        if rendered:
            argv += rendered
            given.add(flag.dest)

    _check_groups(stage, given)
    return argv


def command_argv(
    stage_name: str,
    flags: list[str],
    config_path: Path | None = None,
) -> list[str]:
    """The full child command line, root --config first as the CLI expects."""
    argv = [sys.executable, "-m", "mpt_autopilot.cli"]
    if config_path is not None:
        argv += ["--config", str(config_path)]
    return [*argv, stage_name, *flags]


def build_argv(
    stage: StageSpec,
    form: Mapping[str, object],
    config_path: Path | None = None,
) -> list[str]:
    return command_argv(stage.name, flag_argv(stage, form), config_path)
