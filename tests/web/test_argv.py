"""
Tests for form → argv assembly.

The load-bearing test is the round trip: argv built from a form is fed back to
the real `cli.build_parser()`. If the introspected spec and the assembler ever
disagree with argparse, parsing fails or the Namespace differs here — no
subprocess, no config.yaml and no temp files needed to catch it.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from mpt_autopilot import cli
from mpt_autopilot.web import argv as argv_mod
from mpt_autopilot.web import spec

_SPECS = {s.name: s for s in spec.stage_specs()}


def _parse(stage: str, form: dict[str, object]) -> argparse.Namespace:
    """Build argv from a form, then let the real mpt parser read it back."""
    flags = argv_mod.flag_argv(_SPECS[stage], form)
    return cli.build_parser().parse_args([stage, *flags])


# ── round trips ───────────────────────────────────────────────────────────────


def test_required_picker_round_trips():
    args = _parse("refill", {"lang": "en"})
    assert args.lang == "en"
    assert args.force is False
    assert args.count is None


def test_switch_and_number_round_trip():
    args = _parse("refill", {"lang": "es", "force": True, "count": 7})
    assert (args.lang, args.force, args.count) == ("es", True, 7)


def test_append_flag_round_trips_in_order():
    args = _parse("run", {"langs": ["en", "es"]})
    assert args.langs == ["en", "es"]


def test_append_choice_round_trips():
    args = _parse("run", {"only": ["batch", "enrich"]})
    assert args.only == ["batch", "enrich"]


def test_path_flag_round_trips_as_path():
    args = _parse("enrich", {"dir": "exports_en"})
    assert args.dir == Path("exports_en")


def test_bare_optional_value_flag_uses_argparse_const():
    """`--list-voices` with no value has to reach the stage as const "", not None."""
    args = _parse("batch", {"list_voices": True})
    assert args.list_voices == ""


def test_optional_value_flag_with_a_value():
    args = _parse("batch", {"list_voices": "gemini"})
    assert args.list_voices == "gemini"


def test_upload_post_bare_flag_stays_the_const_not_the_string_true():
    """
    batch's --upload-post has const=True (a bool) with metavar BOOL. Sending
    the checkbox must reach the stage as True, and a typed value as a string.
    """
    assert _parse("batch", {"upload_post": True}).upload_post is True
    assert _parse("batch", {"upload_post": "false"}).upload_post == "false"


def test_required_group_round_trips_either_way():
    assert _parse("upload", {"account": "en"}).account == "en"
    assert _parse("upload", {"all_accounts": True}).all_accounts is True


def test_stage_without_flags():
    assert argv_mod.flag_argv(_SPECS["validate"], {}) == []


# ── values that must not become arguments ─────────────────────────────────────


def test_empty_and_unset_values_are_dropped():
    form: dict[str, object] = {
        "lang": "en",
        "jobs_dir": "",
        "seen_dir": "   ",
        "count": None,
        "force": False,
        "topic": [],
    }
    assert argv_mod.flag_argv(_SPECS["refill"], form) == ["--lang", "en"]


def test_blank_items_inside_an_append_list_are_dropped():
    assert argv_mod.flag_argv(_SPECS["run"], {"langs": ["en", "", "es"]}) == [
        "--lang",
        "en",
        "--lang",
        "es",
    ]


# ── injection ─────────────────────────────────────────────────────────────────


def test_shell_metacharacters_stay_one_opaque_argument():
    payload = "en; rm -rf / && echo $(whoami)"
    built = argv_mod.flag_argv(_SPECS["refill"], {"lang": payload})
    assert built == ["--lang", payload]
    # And argparse hands it to the stage unchanged, as a single value.
    assert _parse("refill", {"lang": payload}).lang == payload


def test_a_value_cannot_smuggle_in_a_second_flag():
    """Even a value that looks like a flag list is one element, not two."""
    built = argv_mod.flag_argv(_SPECS["enrich"], {"lang": "en --force"})
    assert built == ["--lang", "en --force"]
    assert _parse("enrich", {"lang": "en --force"}).force is False


def test_full_argv_runs_the_module_not_a_path_lookup():
    built = argv_mod.build_argv(_SPECS["validate"], {}, Path("cfg.yaml"))
    assert built[1:] == ["-m", "mpt_autopilot.cli", "--config", "cfg.yaml", "validate"]


def test_config_is_omitted_when_the_server_has_no_explicit_path():
    built = argv_mod.build_argv(_SPECS["validate"], {}, None)
    assert "--config" not in built


# ── rejected forms ────────────────────────────────────────────────────────────


def test_unknown_dest_is_rejected():
    with pytest.raises(argv_mod.FormError, match="no flag"):
        argv_mod.flag_argv(_SPECS["batch"], {"nope": "1"})


def test_non_numeric_number_is_rejected():
    with pytest.raises(argv_mod.FormError, match="whole number"):
        argv_mod.flag_argv(_SPECS["refill"], {"lang": "en", "count": "many"})


def test_value_outside_choices_is_rejected():
    with pytest.raises(argv_mod.FormError, match="must be one of"):
        argv_mod.flag_argv(_SPECS["enrich"], {"platform": "vimeo"})


def test_missing_required_flag_is_rejected():
    with pytest.raises(argv_mod.FormError, match="--lang"):
        argv_mod.flag_argv(_SPECS["refill"], {})


def test_empty_required_group_is_rejected():
    with pytest.raises(argv_mod.FormError, match="--account or --all-accounts"):
        argv_mod.flag_argv(_SPECS["upload"], {"dry_run": True})


def test_both_members_of_a_required_group_is_rejected():
    with pytest.raises(argv_mod.FormError, match="only one of"):
        argv_mod.flag_argv(_SPECS["upload"], {"account": "en", "all_accounts": True})


def test_both_members_of_an_optional_group_is_rejected():
    with pytest.raises(argv_mod.FormError, match="cannot be combined"):
        argv_mod.flag_argv(_SPECS["enrich"], {"dir": "exports", "file": "a.mp4"})


def test_switch_sent_as_a_string_is_rejected():
    with pytest.raises(argv_mod.FormError, match="switch"):
        argv_mod.flag_argv(_SPECS["refill"], {"lang": "en", "force": "yes"})


def test_checkbox_state_on_a_value_flag_is_rejected():
    with pytest.raises(argv_mod.FormError, match="takes a value"):
        argv_mod.flag_argv(_SPECS["refill"], {"lang": True})
