"""
Tests for argparse introspection.

The spec is the web UI's only definition of what controls exist, so a silent
drift here means the page stops offering a flag the CLI still accepts. These
tests pin the interesting cases: the flags whose argparse shape does not map
onto one obvious control.

Pure introspection — no config.yaml, no temp files, no subprocesses.
"""

from __future__ import annotations

import argparse

import pytest

from mpt_autopilot import cli
from mpt_autopilot.web import spec


def _spec(name: str) -> spec.StageSpec:
    for candidate in spec.stage_specs():
        if candidate.name == name:
            return candidate
    raise AssertionError(f"no spec for stage {name}")


def test_every_stage_has_a_spec():
    names = {s.name for s in spec.stage_specs()}
    assert names == {
        "refill",
        "init-seen",
        "batch",
        "enrich",
        "upload",
        "run",
        "validate",
        "web",
    }


def test_every_real_action_is_classified():
    """
    Guards the one place argparse forces a class-name check: a rename in a
    future CPython has to fail here, not in a user's browser.
    """
    root = cli.build_parser()
    parsers = [root, *spec._stage_parsers(root).values()]
    for parser in parsers:
        for action in parser._actions:
            assert spec.behaviour_of(action) in {"store", "append", "flag", "skip"}


def test_unclassified_action_raises():
    # A bare Action stands in for any future argparse class the map misses.
    action = argparse.Action(option_strings=["--weird"], dest="weird")
    with pytest.raises(ValueError, match="_ACTION_BEHAVIOURS"):
        spec.behaviour_of(action)


def test_cli_owned_flags_are_not_offered():
    """--config/--help/--version are the CLI's, not a stage's."""
    for stage in spec.stage_specs():
        dests = {flag.dest for flag in stage.flags}
        assert dests.isdisjoint({"stage_config", "config", "help", "version"})


def test_validate_has_no_flags():
    assert _spec("validate").flags == ()


def test_upload_account_group_is_required():
    stage = _spec("upload")
    assert stage.required_exclusive == (("account", "all_accounts"),)
    assert stage.exclusive == ()


def test_enrich_source_group_is_optional():
    stage = _spec("enrich")
    assert stage.exclusive == (("dir", "file"),)
    assert stage.required_exclusive == ()


def test_refill_lang_is_a_required_single_picker():
    flag = _spec("refill").flag("lang")
    assert flag is not None
    assert (flag.kind, flag.multi, flag.required) == ("lang", False, True)


def test_run_lang_is_a_repeatable_picker():
    flag = _spec("run").flag("langs")
    assert flag is not None
    assert (flag.option, flag.kind, flag.multi) == ("--lang", "lang", True)


def test_enrich_lang_is_free_text_not_a_picker():
    """
    `mpt enrich --lang` takes an LLM language name ("English", "ru"), not a
    config.yaml language code — a picker would be narrower than the CLI.
    """
    flag = _spec("enrich").flag("lang")
    assert flag is not None
    assert flag.kind == "text"


def test_optional_value_flags_keep_their_third_state():
    stage = _spec("batch")
    for dest in ("list_voices", "list_bgm", "upload_bgm", "upload_post"):
        flag = stage.flag(dest)
        assert flag is not None, dest
        assert flag.kind == "optional_value", dest


def test_choice_flags_carry_their_choices():
    only = _spec("run").flag("only")
    assert only is not None
    assert (only.kind, only.multi) == ("choice", True)
    assert only.choices == ("refill", "batch", "enrich", "upload")

    platform = _spec("enrich").flag("platform")
    assert platform is not None
    assert platform.choices == ("youtube", "tiktok", "instagram")


def test_advanced_split_keeps_the_everyday_flags_up_front():
    stage = _spec("batch")
    everyday = {f.dest for f in stage.flags if not f.advanced}
    assert everyday == {"lang", "dry_run"}
    assert {"jobs", "seen", "status", "list_voices"} <= {f.dest for f in stage.flags if f.advanced}


def test_required_group_members_are_never_hidden():
    stage = _spec("upload")
    for dest in ("account", "all_accounts"):
        flag = stage.flag(dest)
        assert flag is not None
        assert not flag.advanced, dest


def test_as_dict_is_json_shaped():
    import json

    payload = spec.as_dict(spec.stage_specs(), {"en": {"label": "English"}, "es": {}})
    assert payload["langs"] == [
        {"code": "en", "label": "English"},
        {"code": "es", "label": "es"},
    ]
    # Round-trips without a custom encoder: no tuples, no dataclasses, no Paths.
    assert json.loads(json.dumps(payload)) == payload
