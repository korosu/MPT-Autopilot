"""
web/external.py — mpt runs this page did not start, read-only.

The web layer owns only the runs it spawns itself. A `mpt batch` started in a
terminal is invisible to Runner — but it is still a process on the same
machine, and psutil can see it. This module turns the process table into a
short list of "someone is running X" facts plus, where a stage keeps a file
log (batch, enrich — the only two that construct `Logger(log_file, ...)`),
the tail of it.

What it deliberately does NOT do: control. An external run cannot be
cancelled from here — its stdout belongs to that terminal, and the stages'
own file locks are what keep two runs from fighting over jobs/seen files.

The matcher is conservative on purpose: an editor that merely has cli.py open
must not show up as a run. Only two argv shapes count — the console script
(`mpt` / `mpt.exe`) and `python -m mpt_autopilot.cli` — and one of the known
subcommand names must appear as its own token. A wrapper like
`pwsh -Command "python -m mpt_autopilot.cli batch"` does not match (argv0 is
the shell); the python process it launched matches instead, so no duplicates.

The log-path defaults mirror batch/settings.py (`./logs/batch.log`) and
enricher/settings.py (`log_dir/enricher.log`). They are duplicated rather than
imported because web must not import stage modules — only `cli` (introspection),
shared config, and Flask.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from mpt_autopilot import config as shared_config

# Subcommands that mean "a stage is executing". `web` is excluded on purpose:
# a second `mpt web` is a server, not a run, and listing it would be noise.
_STAGE_NAMES = frozenset({"refill", "init-seen", "batch", "enrich", "upload", "run", "validate"})

_PYTHON = frozenset({"python", "python.exe", "python3", "python3.exe", "py", "py.exe"})
_MPT = frozenset({"mpt", "mpt.exe"})

# How many lines of a stage's file log to show for an external run.
TAIL_LINES = 30


@dataclass(frozen=True)
class ProcRecord:
    """Everything the scanner needs about one process, gathered cheaply."""

    pid: int
    cmdline: list[str]
    create_time: float | None
    cwd: str | None = None


@dataclass(frozen=True)
class ExternalRun:
    pid: int
    stage: str
    started: float | None
    command: str
    config: Path | None
    cwd: str | None = None


def _match(cmdline: list[str]) -> tuple[str, int] | None:
    """
    `(stage, marker)` when `cmdline` is an mpt invocation, else None.

    `marker` is the index of the `mpt`/`mpt_autopilot.cli` token — everything
    stage-relevant starts after it, so interpreter flags never confuse the
    subcommand search. A `--config` pair is skipped during that search: a
    config file literally named after a stage must not be mistaken for one.
    """
    if not cmdline:
        return None
    # Split on both separators: a Windows install (`C:\...\mpt.exe`) scanned
    # from a Linux server must still reduce to `mpt.exe`, and vice versa.
    argv0 = cmdline[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if argv0 in _MPT:
        marker = 0
    else:
        marker = -1
        for i in range(len(cmdline) - 1):
            if cmdline[i] == "-m" and cmdline[i + 1] == "mpt_autopilot.cli":
                if argv0 not in _PYTHON:
                    return None  # a shell wrapper, not the interpreter itself
                marker = i + 1
                break
        if marker < 0:
            return None

    index = marker + 1
    while index < len(cmdline):
        token = cmdline[index]
        if token == "--config":
            index += 2  # the flag and its value: neither is the subcommand
            continue
        if token in _STAGE_NAMES:
            return token, marker
        index += 1
    return None


def _skip_config_pairs(tokens: list[str]) -> list[str]:
    """Drop `--config <path>` pairs: the command shown should read like the
    user typed it, not like the server received it (app.py's own echo does the
    same for its children)."""
    out: list[str] = []
    skip = False
    for token in tokens:
        if skip:
            skip = False
            continue
        if token == "--config":
            skip = True
            continue
        out.append(token)
    return out


def _display(cmdline: list[str], marker: int) -> str:
    tokens = _skip_config_pairs(cmdline[marker + 1 :])
    return " ".join(tokens)


def _config_of(cmdline: list[str], marker: int) -> Path | None:
    for i in range(marker + 1, len(cmdline) - 1):
        if cmdline[i] == "--config":
            return Path(cmdline[i + 1])
    return None


def scan(
    records: list[ProcRecord],
    *,
    exclude_pids: set[int] | None = None,
    self_pid: int | None = None,
) -> list[ExternalRun]:
    """The mpt runs among `records`, newest first."""
    exclude = set(exclude_pids or set())
    if self_pid is not None:
        exclude.add(self_pid)

    runs: list[ExternalRun] = []
    for record in records:
        match = _match(record.cmdline)
        if match is None or record.pid in exclude:
            continue
        stage, marker = match
        runs.append(
            ExternalRun(
                pid=record.pid,
                stage=stage,
                started=record.create_time,
                command=_display(record.cmdline, marker),
                config=_config_of(record.cmdline, marker),
                cwd=record.cwd,
            )
        )
    runs.sort(key=lambda run: (run.started is None, -(run.started or 0)))
    return runs


def psutil_records() -> list[ProcRecord] | None:
    """
    One pass over the process table, or None when psutil is missing (an env
    synced before the [web] extra grew it). cwd is left out here — it costs a
    syscall per process — and attached by fill_cwd for the few that matched.
    """
    try:
        import psutil
    except ImportError:
        return None

    records: list[ProcRecord] = []
    for proc in psutil.process_iter(["pid", "cmdline", "create_time"]):
        info = proc.info
        cmdline = info.get("cmdline") or []
        records.append(
            ProcRecord(
                pid=info["pid"],
                cmdline=list(cmdline),
                create_time=info.get("create_time"),
            )
        )
    return records


def fill_cwd(runs: list[ExternalRun]) -> list[ExternalRun]:
    """Attach each run's working directory (psutil), so a run started without
    an explicit --config can have its `./config.yaml` located. Best effort:
    AccessDenied or a vanished process leaves cwd None."""
    try:
        import psutil
    except ImportError:
        return runs

    filled: list[ExternalRun] = []
    for run in runs:
        if run.config is not None or run.pid is None:
            filled.append(run)
            continue
        try:
            cwd = psutil.Process(run.pid).cwd()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            filled.append(run)
            continue
        filled.append(replace(run, cwd=cwd))
    return filled


def applicable_logs(cfg: shared_config.Config, stage: str) -> list[tuple[str, Path]]:
    """The stage-log files that exist for this stage: batch writes
    `batch.log_file` (default ./logs/batch.log), enrich writes
    `log_dir/enricher.log` (default ./logs). `mpt run` produces both, since
    the pipeline runs those stages in-process."""
    batch_log = cfg.resolve(str(cfg.section("batch").get("log_file", "./logs/batch.log")))
    enrich_log = cfg.resolve(str(cfg.section("enricher").get("log_dir", "./logs"))) / "enricher.log"
    if stage == "batch":
        return [("batch", batch_log)]
    if stage == "enrich":
        return [("enrich", enrich_log)]
    if stage == "run":
        return [("batch", batch_log), ("enrich", enrich_log)]
    return []


def tail(path: Path, lines: int = TAIL_LINES) -> str | None:
    """The last `lines` of a log file, or None when it does not exist."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return "\n".join(text.splitlines()[-lines:])


def _same_config(a: Path, b: Path) -> bool:
    # Windows paths compare case-insensitively; resolve() is non-strict, so a
    # not-yet-created path still normalizes.
    return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))


def _resolved_config(run: ExternalRun) -> Path | None:
    # No --config on the command line means the CLI fell back to
    # ./config.yaml in its own working directory (cli.py's default).
    if run.config is None:
        if not run.cwd:
            return None
        return (Path(run.cwd) / "config.yaml").resolve()
    path = run.config
    if not path.is_absolute() and run.cwd:
        path = Path(run.cwd) / path
    return path.resolve()


def run_payload(
    run: ExternalRun, cfg: shared_config.Config, config_path: Path
) -> dict[str, object]:
    """
    One external run as JSON, with log tails when — and only when — the run
    provably uses the same config this page was started with. Guessing the
    config would show the wrong file as "what it is doing".
    """
    logs: list[dict[str, str]] = []
    resolved = _resolved_config(run)
    if resolved is not None and _same_config(resolved, config_path):
        for name, path in applicable_logs(cfg, run.stage):
            body = tail(path)
            if body is not None:
                logs.append({"name": name, "path": str(path), "tail": body})
    return {
        "pid": run.pid,
        "stage": run.stage,
        "started": run.started,
        "command": run.command,
        "logs": logs,
    }
