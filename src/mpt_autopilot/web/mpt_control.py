"""
web/mpt_control.py — the Settings page's narrow control surface over MPT.

Three facts the Settings page needs that no other module has:

  * which commands may be run against the machine MPT lives on — and ONLY
    those, from one of two places. config.yaml's `web.mpt_restart_command`
    / `web.mpt_logs_command` (the power-user escape hatch: full command
    lines, highest priority), or the setup wizard's choice in its own
    web-settings.yaml next to it. The wizard is the point of the page: no
    one should have to hand-write `sudo systemctl restart moneyprinter` —
    pick a deployment type (systemd / docker / tmux), name the thing, and
    the commands are assembled from validated single-line fields. The page
    never executes anything the user did not put in one of those two files
    first.
  * the version behind the port (GET /openapi.json), so "up" can also say
    "up, MPT 1.3.3" — and mean it.
  * WHO owns the port: a python listener is MPT itself, nothing means the
    dot is about to go red. MPT Autopilot runs on the same server as MPT,
    so the interesting answers are "here" and "nobody".

Restart runs to completion synchronously (a systemctl restart returns in
seconds); logs stream — the command is typically `journalctl -f`, which
never exits on its own, so CommandStream pumps it through the same
buffer+subscribers shape as runner.Runner. Deliberately NOT Runner itself:
the run slot belongs to stages, and watching MPT's log must work while a
render is in progress. tmux has no follow mode at all, so its "logs" is no
command — TmuxPollStream polls `tmux capture-pane` and emits only the new
lines through the same buffer.

shell=True throughout: these are full command lines the user authored — or
the wizard assembled from the user's validated single-line answers — like
`sudo systemctl restart moneyprinter`; splitting them would break exactly
the quoting they depend on. The trust boundary is config.yaml plus
web-settings.yaml, files that already hold every API key and restart
command.

No Flask here, so tests need no [web] extra — same split as status.py.
"""

from __future__ import annotations

import os
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml

from mpt_autopilot import config as shared_config
from mpt_autopilot.web.runner import ChildProcess, kill_tree

# A restart is a systemctl call: seconds. A timeout means the service or the
# box is wedged — report it instead of hanging the request thread.
RESTART_TIMEOUT_SECONDS = 60.0

# Same short budget as status.py's probe: this runs on a button click while
# the user watches, and the port is usually loopback.
OPENAPI_TIMEOUT_SECONDS = 2.0

DEFAULT_BUFFER_LINES = 2000

# The loopback spellings that mean "the port is on THIS machine, so scanning
# the process table will name its owner". Anything else (a LAN/VPN IP) means
# deployment() must not pretend to know.
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


# ── config keys ─────────────────────────────────────────────────────────────


def _command(cfg: shared_config.Config, key: str) -> str | None:
    """One of `web.mpt_restart_command` / `web.mpt_logs_command`, or None.

    Tolerant on purpose: a missing or malformed `web:` section (a scalar, the
    key set to a list) reads as "not configured", never as a broken page.
    """
    try:
        value = cfg.section("web").get(key)
    except shared_config.ConfigError:
        return None
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def restart_command(cfg: shared_config.Config) -> str | None:
    return _command(cfg, "mpt_restart_command")


def logs_command(cfg: shared_config.Config) -> str | None:
    return _command(cfg, "mpt_logs_command")


# ── the setup wizard: deployment types, derived commands, web-settings.yaml ─

# The wizard's own file, next to config.yaml. config.yaml is never rewritten
# from the page — its comments are the user's — so the page owns this one.
WEB_SETTINGS_FILENAME = "web-settings.yaml"

DEPLOYMENT_TYPES = ("systemd", "docker", "tmux")


@dataclass(frozen=True)
class MptDeployment:
    """The wizard's answer: how MPT runs, in the user's own words.

    `name` is the unit / container / session; `start_command` only for
    tmux (restart = kill the session, then start it again the way the
    user does).
    """

    type: str
    name: str
    start_command: str | None = None


def deployment_settings_path(cfg: shared_config.Config) -> Path:
    return cfg.dir / WEB_SETTINGS_FILENAME


def validate_deployment(payload: Mapping[str, object]) -> MptDeployment:
    """
    Check the wizard form and shape it into an MptDeployment.

    Every field must be a non-empty single line: the derived commands are
    assembled into full shell lines, so a newline in a field would splice a
    second command in — the one injection the wizard must not have. Raises
    ValueError with a clean message; the route turns that into a 400.
    """
    if not isinstance(payload, Mapping):
        raise ValueError("the deployment must be a mapping")
    kind = payload.get("type")
    if not isinstance(kind, str) or kind not in DEPLOYMENT_TYPES:
        raise ValueError("type must be one of: " + ", ".join(DEPLOYMENT_TYPES))
    name = _single_line(payload.get("name"), "name")
    start_command = None
    if kind == "tmux":
        start_command = _single_line(payload.get("start_command"), "start_command")
    return MptDeployment(type=kind, name=name, start_command=start_command)


def _single_line(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} is required")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field} must not be blank")
    if "\n" in stripped or "\r" in stripped:
        raise ValueError(f"{field} must be a single line")
    return stripped


def load_deployment(cfg: shared_config.Config) -> MptDeployment | None:
    """
    The saved wizard choice, or None.

    A missing file, unreadable YAML, a wrong-shaped document, or a choice
    that would not validate today all read as None: the page degrades to
    its unconfigured hints, never a traceback.
    """
    path = deployment_settings_path(cfg)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if not isinstance(data, Mapping):
        return None
    choice = data.get("deployment")
    if not isinstance(choice, Mapping):
        return None
    try:
        return validate_deployment(choice)
    except ValueError:
        return None


def save_deployment(cfg: shared_config.Config, deployment: MptDeployment) -> None:
    """
    Write the wizard choice — atomic tmp+replace, so a crash mid-write can
    never leave a half file for load_deployment to trip over.
    """
    choice: dict[str, str] = {"type": deployment.type, "name": deployment.name}
    if deployment.start_command is not None:
        choice["start_command"] = deployment.start_command
    path = deployment_settings_path(cfg)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(yaml.safe_dump({"deployment": choice}, sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)


def derived_restart(deployment: MptDeployment) -> str:
    """The restart line each deployment type promises."""
    if deployment.type == "docker":
        return f"docker restart {deployment.name}"
    if deployment.type == "tmux":
        return f"tmux kill-session -t {deployment.name} && {deployment.start_command}"
    return f"sudo systemctl restart {deployment.name}"


def derived_logs(deployment: MptDeployment) -> str | None:
    """
    The logs line, or None for tmux.

    journalctl deliberately runs WITHOUT sudo: sudo without a TTY dies on
    the password prompt and takes the stream with it, and a user in the
    journal group can read their service's logs anyway. None means "no
    command can follow this" — TmuxPollStream polls the pane instead.
    """
    if deployment.type == "docker":
        return f"docker logs -f {deployment.name}"
    if deployment.type == "tmux":
        return None
    return f"journalctl -u {deployment.name} -f"


@dataclass(frozen=True)
class ResolvedMptCommands:
    """What the Settings buttons actually run, and where each answer came from.

    `logs_kind` tells the logs route which machinery to start: "command"
    (a follow-mode line to stream), "tmux" (a pane to poll), or "none".
    """

    restart: str | None
    logs: str | None
    logs_kind: str
    deployment: MptDeployment | None


def resolve_commands(cfg: shared_config.Config) -> ResolvedMptCommands:
    """
    The resolution chain, per command: config.yaml's manual keys first,
    the wizard's derived lines second — a power user's hand-tuned line is
    never silently overwritten by the form.
    """
    deployment = load_deployment(cfg)
    restart = restart_command(cfg)
    if restart is None and deployment is not None:
        restart = derived_restart(deployment)

    logs = logs_command(cfg)
    if logs is not None:
        return ResolvedMptCommands(restart, logs, "command", deployment)
    if deployment is None:
        return ResolvedMptCommands(restart, None, "none", None)
    if deployment.type == "tmux":
        return ResolvedMptCommands(restart, None, "tmux", deployment)
    return ResolvedMptCommands(restart, derived_logs(deployment), "command", deployment)


# ── version + deployment facts for the status card ─────────────────────────


def openapi_version(
    api_url: str | None,
    timeout: float = OPENAPI_TIMEOUT_SECONDS,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, str] | None:
    """
    `{"version", "title"}` from GET {api_url}/openapi.json, or None.

    FastAPI serves this unauthenticated on every known MPT release (verified
    against MPT 1.3.3: `info.version` is "1.3.3", `info.title` is
    "MoneyPrinterTurbo"). The title rides along so a port squatter that also
    speaks OpenAPI cannot be presented as "MPT version X" — the card shows
    exactly what the server claimed, not what we hope it is.
    """
    if not api_url:
        return None
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            response = client.get(f"{api_url.rstrip('/')}/openapi.json")
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        info: object = response.json().get("info", {})
    except ValueError:
        return None
    # A body whose "info" is not an object is not an OpenAPI document.
    if not isinstance(info, dict):
        return None
    version = info.get("version")
    title = info.get("title")
    if not isinstance(version, str) or not isinstance(title, str):
        return None
    return {"version": version, "title": title}


def psutil_port_holder(port: int) -> dict[str, object] | None:
    """
    The process listening on `port`: `{"pid", "name"}`, or None.

    Raises ImportError when psutil is not installed (an env synced before the
    [web] extra grew it) — the caller renders that as "unknown" rather than
    guessing. AccessDenied falls back the same way: on some Windows set-ups
    net_connections() needs elevation, and a settings page that crashes on
    permissions helps nobody.
    """
    import psutil

    for conn in psutil.net_connections(kind="tcp"):
        if conn.status != psutil.CONN_LISTEN:
            continue
        if not conn.laddr or conn.laddr.port != port:
            continue
        if conn.pid is None:
            return {"pid": None, "name": ""}
        try:
            name = psutil.Process(conn.pid).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return {"pid": conn.pid, "name": ""}
        return {"pid": conn.pid, "name": name}
    return None


def deployment(
    api_url: str | None,
    *,
    port_holder: Callable[[int], dict[str, object] | None] | None = None,
) -> str:
    """
    One word for what owns `api_url`'s port — the card's "where is MPT?".

    "unconfigured" | "remote-host" | "port-free" | "local" |
    "other: <name>" | "unknown". MPT Autopilot runs on the same server as
    MPT, so the answers that matter are "local" (a python listener — MPT
    itself) and "port-free" (the dot is about to go red).
    """
    if not api_url:
        return "unconfigured"
    parsed = urlsplit(api_url)
    hostname = (parsed.hostname or "").lower()
    if hostname not in _LOCAL_HOSTS:
        return "remote-host"
    port = parsed.port
    if port is None:
        return "unknown"
    lookup = port_holder or psutil_port_holder
    try:
        holder = lookup(port)
    except (ImportError, OSError, RuntimeError):
        return "unknown"
    if holder is None:
        return "port-free"
    name = str(holder.get("name") or "")
    if not name:
        return "unknown"
    lowered = name.lower()
    if "python" in lowered:
        return "local"
    return f"other: {name}"


# ── restart ─────────────────────────────────────────────────────────────────


def run_restart(
    command: str, cwd: Path, timeout: float = RESTART_TIMEOUT_SECONDS
) -> dict[str, object]:
    """
    Run `web.mpt_restart_command` to completion and report what it said.

    Output arrives as a list of lines so the page can append without
    re-splitting. ok=False carries the exit code and the tail anyway: a
    failed restart whose error message is visible beats a green checkmark
    hiding a dead service.
    """
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "exit_code": None,
            "output": [f"[web] restart command timed out after {timeout:g}s"],
        }
    except OSError as exc:
        return {"ok": False, "exit_code": None, "output": [f"[web] could not run: {exc}"]}
    output = (completed.stdout + completed.stderr).splitlines()
    return {"ok": completed.returncode == 0, "exit_code": completed.returncode, "output": output}


# ── live logs ───────────────────────────────────────────────────────────────


def spawn(command: str, cwd: Path | None = None) -> subprocess.Popen[str]:
    """
    Start the logs command with a pipe that yields text as it is produced.

    Same shape as runner.spawn(): line-buffered text, errors replaced, the
    child in its own session so stop() can kill the group (an `ssh …
    journalctl -f` leaves the remote side to its own timeouts).
    """
    return subprocess.Popen(
        command,
        cwd=cwd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        start_new_session=True,
    )


class _LineFanout:
    """
    Numbered lines, late subscribers, keepalives — the shape runner.Runner,
    CommandStream and TmuxPollStream share.

    `_append` assigns ids under the condition; `subscribe(last_id)` replays
    everything the subscriber missed, waits with keepalive None ticks while
    the stream is alive, and returns once the stream has finished AND the
    subscriber is caught up. Each stream keeps its own liveness (a child
    process vs a poll thread), so is_live/stop stay per class.
    """

    def __init__(self, buffer_lines: int = DEFAULT_BUFFER_LINES) -> None:
        self._cond = threading.Condition()
        self._lines: deque[tuple[int, str]] = deque(maxlen=buffer_lines)
        self._last_line = 0
        self._finished = False

    def subscribe(self, last_id: int = 0) -> Iterator[tuple[int, str] | None]:
        """
        Yield `(line_id, text)` until the stream ends; None = keepalive tick.

        Same contract as runner.Runner.subscribe so the SSE route reads the
        same for every stream kind. The buffer holds only this stream's
        lines, so a subscriber joining late replays from the beginning
        (journalctl's own recent lines), which is what "show me the logs"
        means.
        """
        while True:
            with self._cond:
                pending = self._since(last_id)
                if not pending and not self._finished:
                    self._cond.wait(timeout=15.0)
                    pending = self._since(last_id)
                finished = self._finished

            for item in pending:
                last_id = item[0]
                yield item

            if not pending:
                if finished:
                    return
                yield None

    def _since(self, last_id: int) -> list[tuple[int, str]]:
        return [item for item in self._lines if item[0] > last_id]

    def _append(self, text: str) -> None:
        with self._cond:
            self._last_line += 1
            self._lines.append((self._last_line, text))
            self._cond.notify_all()


class CommandStream(_LineFanout):
    """
    One live logs command, fanned out to any number of SSE subscribers.

    The buffer+Condition shape is runner.Runner's, minus everything run-shaped
    (no slot juggling — at most one logs command at a time, guarded by the
    caller; no lock sweep — this child holds no mpt locks).
    """

    def __init__(
        self,
        command: str,
        cwd: Path,
        spawner: Callable[[str], ChildProcess] | None = None,
        buffer_lines: int = DEFAULT_BUFFER_LINES,
    ) -> None:
        super().__init__(buffer_lines)
        self._command = command
        self._cwd = cwd
        self._spawn = spawner or (lambda cmd: spawn(cmd, cwd))
        self._proc: ChildProcess | None = None

    def start(self) -> None:
        """Spawn the command and begin pumping. One shot: restart = new stream."""
        proc = self._spawn(self._command)
        with self._cond:
            if self._finished or self._proc is not None:
                kill_tree(proc)
                raise RuntimeError("this stream already ran — start a new one")
            self._proc = proc
            self._cond.notify_all()
        threading.Thread(
            target=self._pump,
            args=(proc,),
            name=f"mpt-web-mpt-logs-{proc.pid}",
            daemon=True,
        ).start()

    def is_live(self) -> bool:
        with self._cond:
            return self._proc is not None and not self._finished

    def stop(self) -> bool:
        """Kill the command and its children. True if there was something to stop."""
        with self._cond:
            proc = self._proc
            if proc is None or self._finished:
                return False
        kill_tree(proc)
        return True

    def _pump(self, proc: ChildProcess) -> None:
        try:
            if proc.stdout is not None:
                for line in proc.stdout:
                    self._append(line.rstrip("\r\n"))
            proc.wait()
        except (OSError, ValueError) as exc:
            self._append(f"[web] lost the MPT log stream: {exc}")
        finally:
            with self._cond:
                self._finished = True
                self._proc = None
                self._cond.notify_all()


# ── tmux logs: a pane has no follow mode, so the page polls it ──────────────

POLL_INTERVAL_SECONDS = 2.0
CAPTURE_WINDOW_LINES = 200

_PANE_RESYNCED = "[web] the pane scrolled past the capture window; resynced"


def _capture_pane(session: str, window: int = CAPTURE_WINDOW_LINES) -> list[str] | None:
    """
    `tmux capture-pane -p -t SESSION -S -window`, or None when it failed.

    None (never []) means "could not capture" — the session may be gone
    mid-restart — so the poller reports it and keeps trying; an empty pane
    is a valid capture.
    """
    try:
        completed = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", session, "-S", f"-{window}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.splitlines()


def _pane_new_lines(old: list[str], pane: list[str]) -> list[str]:
    """
    Only the lines `pane` grew since `old`.

    Anchored on old's last line, the way a person reads a pane: appends
    land after it, a scrolled window keeps it somewhere inside. When the
    anchor scrolled out entirely (more new lines than the capture window
    holds), say so and replay the whole pane — a log viewer must neither
    skip lines silently nor repaint them silently.
    """
    if not old:
        return list(pane)
    try:
        anchor_at = pane.index(old[-1])
    except ValueError:
        return [_PANE_RESYNCED] + list(pane)
    return pane[anchor_at + 1 :]


class TmuxPollStream(_LineFanout):
    """
    A tmux pane as a live stream: poll capture-pane, emit only what's new.

    Same buffer and subscribers as CommandStream, but nothing is killed on
    stop() — the poller simply walks away from the pane. A failed capture
    (session renamed, server down mid-restart) is reported once and
    retried: the pane coming back after a restart is the whole point of
    watching it.
    """

    def __init__(
        self,
        session: str,
        *,
        interval: float = POLL_INTERVAL_SECONDS,
        capture: Callable[[str], list[str] | None] | None = None,
        buffer_lines: int = DEFAULT_BUFFER_LINES,
    ) -> None:
        super().__init__(buffer_lines)
        self._session = session
        self._interval = interval
        self._capture = capture or _capture_pane
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Begin polling. One shot: restarting the watch = new poller."""
        if self._thread is not None:
            raise RuntimeError("this poller already ran — start a new one")
        self._thread = threading.Thread(
            target=self._poll,
            name=f"mpt-web-tmux-{self._session}",
            daemon=True,
        )
        self._thread.start()

    def is_live(self) -> bool:
        with self._cond:
            return self._thread is not None and not self._finished

    def stop(self) -> bool:
        """Stop polling. True if there was something to stop."""
        with self._cond:
            if self._thread is None or self._finished:
                return False
        self._stop.set()
        return True

    def _poll(self) -> None:
        last: list[str] = []
        failed = False
        try:
            while True:
                pane = self._capture(self._session)
                if pane is None:
                    if not failed:
                        failed = True
                        self._append(
                            f"[web] tmux capture failed — is the session '{self._session}' running?"
                        )
                else:
                    if failed:
                        failed = False
                        self._append("[web] the pane is back")
                    for line in _pane_new_lines(last, pane):
                        self._append(line)
                    last = pane
                if self._stop.wait(self._interval):
                    break
        finally:
            with self._cond:
                self._finished = True
                self._cond.notify_all()
