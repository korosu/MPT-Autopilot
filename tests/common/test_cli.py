"""
Tests for the `mpt` CLI wiring: every stage is registered, --config is accepted
before and after the subcommand, and help never needs a config.yaml on disk.

The last point matters in practice: the enricher keeps its settings behind a
lazy singleton precisely so `mpt enrich --help` works in a directory with no
config.yaml. A regression there would only show up as a crash for new users.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mpt_autopilot import cli


def test_all_stages_are_registered():
    parser = cli.build_parser()
    # argparse keeps the subparsers action last in _subparsers._group_actions.
    choices = set()
    for action in parser._actions:
        if action.dest == "stage" and action.choices:
            choices = set(action.choices)
    assert choices == {"refill", "init-seen", "batch", "enrich", "upload", "run"}


def test_every_stage_exposes_the_hook_pair():
    for name, _help in cli.STAGES:
        hooks = cli._stage_hooks(name)
        assert callable(hooks.add_arguments)
        assert callable(hooks.execute)
        assert hooks.epilog.strip(), f"{name} has no examples in its EPILOG"


def test_unknown_stage_hook_raises():
    with pytest.raises(ValueError, match="unknown stage"):
        cli._stage_hooks("nope")


@pytest.mark.parametrize("stage", ["refill", "init-seen", "batch", "enrich", "upload", "run"])
def test_stage_help_shows_the_stage_examples(stage, tmp_path, monkeypatch, capsys):
    """Each stage's EPILOG has to reach `mpt <stage> --help`, not just its standalone parser."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        cli.main([stage, "--help"])
    out = capsys.readouterr().out
    assert "Examples:" in out
    assert f"mpt {stage}" in out


def test_root_help_needs_no_config(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # deliberately empty: no config.yaml here
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    assert "mpt" in capsys.readouterr().out


@pytest.mark.parametrize("stage", ["refill", "init-seen", "batch", "enrich", "upload", "run"])
def test_stage_help_needs_no_config(stage, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        cli.main([stage, "--help"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip()


def test_no_subcommand_prints_help_and_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main([]) == 1
    assert "usage" in capsys.readouterr().out.lower()


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert "mpt-autopilot" in capsys.readouterr().out


def test_root_config_reaches_the_stage(monkeypatch, tmp_path):
    seen: dict[str, Path | None] = {}
    real_hooks = cli._stage_hooks

    def hooks(name):
        real = real_hooks(name)

        def fake_execute(args, config_path):
            seen["config"] = config_path
            return 0

        return real._replace(execute=fake_execute)

    monkeypatch.setattr(cli, "_stage_hooks", hooks)

    config = tmp_path / "custom.yaml"
    assert cli.main(["--config", str(config), "batch"]) == 0
    assert seen["config"] == config


def test_stage_config_wins_over_root_config(monkeypatch, tmp_path):
    seen: dict[str, Path | None] = {}
    real_hooks = cli._stage_hooks

    def hooks(name):
        real = real_hooks(name)

        def fake_execute(args, config_path):
            seen["config"] = config_path
            return 0

        return real._replace(execute=fake_execute)

    monkeypatch.setattr(cli, "_stage_hooks", hooks)

    root = tmp_path / "root.yaml"
    stage = tmp_path / "stage.yaml"
    assert cli.main(["--config", str(root), "batch", "--config", str(stage)]) == 0
    assert seen["config"] == stage


def test_stage_exit_code_is_propagated(monkeypatch):
    real_hooks = cli._stage_hooks

    def hooks(name):
        return real_hooks(name)._replace(execute=lambda args, config_path: 2)

    monkeypatch.setattr(cli, "_stage_hooks", hooks)
    assert cli.main(["batch"]) == 2
