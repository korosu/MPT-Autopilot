"""
Tests for external-run detection.

The matcher and the payload builder are pure: a fake process record is a
frozen dataclass, so no process is started and no psutil call is needed —
except the one fill_cwd test, which inspects this very pytest process.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mpt_autopilot import config as shared_config
from mpt_autopilot.web import external as external_mod
from mpt_autopilot.web.external import ExternalRun, ProcRecord, scan


def _record(pid: int, *tokens: str, create_time: float | None = None) -> ProcRecord:
    return ProcRecord(pid=pid, cmdline=list(tokens), create_time=create_time)


def _cfg(tmp_path: Path, body: str) -> shared_config.Config:
    config = tmp_path / "config.yaml"
    config.write_text(body, encoding="utf-8")
    return shared_config.load(config, env_path=config, reload=True)


# ── the matcher ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cmdline, expected",
    [
        (["C:\\venv\\Scripts\\mpt.exe", "batch", "--lang", "en"], "batch"),
        (["/usr/local/bin/mpt", "run", "--skip", "upload"], "run"),
        (["python", "-m", "mpt_autopilot.cli", "--config", "x.yaml", "enrich"], "enrich"),
        (["python.exe", "-m", "mpt_autopilot.cli", "refill"], "refill"),
        # a config file literally named after a stage is not the subcommand
        (["mpt", "--config", "run", "validate"], "validate"),
        # an editor that merely has cli.py open is not a run
        (["C:\\editors\\Code.exe", "D:\\proj\\mpt_autopilot\\cli.py"], None),
        # a shell wrapping the real process is not the process
        (["pwsh.exe", "-Command", "python -m mpt_autopilot.cli batch"], None),
        # the web server is a server, not a run
        (["python", "-m", "mpt_autopilot.cli", "--config", "x.yaml", "web"], None),
        # an unrelated python -m module is not a run
        (["python", "-m", "http.server"], None),
        # no subcommand at all
        (["python", "-m", "mpt_autopilot.cli"], None),
        ([], None),
    ],
)
def test_the_matcher_recognizes_only_real_mpt_invocations(cmdline, expected):
    match = external_mod._match(cmdline)
    assert (match[0] if match else None) == expected


def test_the_command_display_reads_like_what_was_typed():
    records = [
        _record(
            7,
            "python",
            "-m",
            "mpt_autopilot.cli",
            "--config",
            "D:\\x\\config.yaml",
            "batch",
            "--lang",
            "en",
        )
    ]
    run = scan(records, self_pid=99)[0]
    assert run.command == "batch --lang en"
    assert run.config == Path("D:\\x\\config.yaml")


def test_scan_finds_runs_newest_first_and_skips_excluded_pids():
    records = [
        _record(10, "mpt.exe", "batch", create_time=100.0),
        _record(20, "mpt.exe", "enrich", create_time=200.0),
        _record(30, "mpt.exe", "validate", create_time=300.0),
    ]
    runs = scan(records, exclude_pids={30}, self_pid=99)
    assert [(run.pid, run.stage) for run in runs] == [(20, "enrich"), (10, "batch")]


def test_scan_excludes_the_server_itself():
    records = [_record(os.getpid(), "mpt", "batch")]
    assert scan(records, self_pid=os.getpid()) == []


# ── cwd filling (the one real psutil call) ───────────────────────────────────


def test_fill_cwd_attaches_the_process_working_directory():
    run = ExternalRun(pid=os.getpid(), stage="batch", started=1.0, command="batch", config=None)
    filled = external_mod.fill_cwd([run])[0]
    assert filled.cwd is not None
    assert os.path.normcase(filled.cwd) == os.path.normcase(os.getcwd())


def test_fill_cwd_ignores_runs_that_name_their_config():
    run = ExternalRun(
        pid=os.getpid(), stage="batch", started=1.0, command="batch", config=Path("x.yaml")
    )
    assert external_mod.fill_cwd([run])[0].cwd is None


# ── stage log files ──────────────────────────────────────────────────────────


def test_batch_and_enrich_name_their_log_files(tmp_path):
    cfg = _cfg(tmp_path, "batch:\n  log_file: ./logs/batch.log\nenricher:\n  log_dir: ./logs\n")
    (tmp_path / "logs").mkdir()
    expected = tmp_path / "logs" / "batch.log"
    logs = external_mod.applicable_logs(cfg, "batch")
    assert [name for name, _ in logs] == ["batch"]
    assert logs[0][1] == expected.resolve()


def test_a_pipeline_run_carries_both_file_logs(tmp_path):
    cfg = _cfg(tmp_path, "batch:\n  log_file: ./logs/batch.log\nenricher:\n  log_dir: ./logs\n")
    assert [name for name, _ in external_mod.applicable_logs(cfg, "run")] == ["batch", "enrich"]


def test_a_stage_without_a_file_log_lists_nothing(tmp_path):
    cfg = _cfg(tmp_path, "batch:\n  log_file: ./logs/batch.log\n")
    assert external_mod.applicable_logs(cfg, "refill") == []


def test_tail_reads_the_last_lines_only(tmp_path):
    path = tmp_path / "batch.log"
    path.write_text("\n".join(f"line{i}" for i in range(50)), encoding="utf-8")
    text = external_mod.tail(path, lines=5)
    assert text is not None
    assert text.splitlines() == [f"line{i}" for i in range(45, 50)]


def test_tail_is_none_for_a_missing_file(tmp_path):
    assert external_mod.tail(tmp_path / "nope.log") is None


# ── payload ──────────────────────────────────────────────────────────────────


def test_run_payload_tails_the_log_for_a_run_on_the_same_config(tmp_path):
    cfg = _cfg(tmp_path, "batch:\n  log_file: ./logs/batch.log\n")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "batch.log").write_text("working\n", encoding="utf-8")

    run = ExternalRun(
        pid=11, stage="batch", started=123.0, command="batch", config=tmp_path / "config.yaml"
    )
    payload = external_mod.run_payload(run, cfg, tmp_path / "config.yaml")
    assert payload["pid"] == 11
    assert payload["stage"] == "batch"
    logs = payload["logs"]
    assert isinstance(logs, list)
    assert [log["name"] for log in logs] == ["batch"]
    assert logs[0]["tail"] == "working"


def test_run_payload_resolves_a_config_from_the_runs_cwd(tmp_path):
    """A run started without --config still points at ./config.yaml."""
    cfg = _cfg(tmp_path, "batch:\n  log_file: ./logs/batch.log\n")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "batch.log").write_text("working\n", encoding="utf-8")

    run = ExternalRun(
        pid=11, stage="batch", started=123.0, command="batch", config=None, cwd=str(tmp_path)
    )
    payload = external_mod.run_payload(run, cfg, tmp_path / "config.yaml")
    logs = payload["logs"]
    assert isinstance(logs, list)
    assert logs[0]["tail"] == "working"


def test_run_payload_stays_quiet_when_the_config_differs(tmp_path):
    cfg = _cfg(tmp_path, "batch:\n  log_file: ./logs/batch.log\n")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "batch.log").write_text("working\n", encoding="utf-8")

    run = ExternalRun(
        pid=11, stage="batch", started=123.0, command="batch", config=tmp_path / "other.yaml"
    )
    payload = external_mod.run_payload(run, cfg, tmp_path / "config.yaml")
    assert payload["logs"] == []


def test_a_pipeline_run_payload_carries_both_log_tails(tmp_path):
    cfg = _cfg(tmp_path, "batch:\n  log_file: ./logs/batch.log\nenricher:\n  log_dir: ./logs\n")
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "batch.log").write_text("rendering\n", encoding="utf-8")
    (logs_dir / "enricher.log").write_text("tagging\n", encoding="utf-8")

    run = ExternalRun(
        pid=11, stage="run", started=123.0, command="run", config=tmp_path / "config.yaml"
    )
    payload = external_mod.run_payload(run, cfg, tmp_path / "config.yaml")
    logs = payload["logs"]
    assert isinstance(logs, list)
    assert [(log["name"], log["tail"]) for log in logs] == [
        ("batch", "rendering"),
        ("enrich", "tagging"),
    ]


def test_fill_cwd_without_psutil_is_a_no_op(monkeypatch):
    """An env synced before the extra grew psutil must not crash the page."""
    import builtins

    real_import = builtins.__import__

    def no_psutil(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("no psutil here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_psutil)
    run = ExternalRun(pid=1, stage="batch", started=1.0, command="batch", config=None)
    assert external_mod.fill_cwd([run]) == [run]
    assert external_mod.psutil_records() is None
