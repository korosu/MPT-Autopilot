"""
web/cli.py — `mpt web`, a local browser front-end for every other stage.

Flask is an optional dependency (`uv sync --extra web`) and
`cli.build_parser()` imports this module on every `mpt --help`. Nothing here
may import Flask — or anything that imports Flask — at module scope, or
`mpt --help` breaks for everyone who skipped the extra. The import lives inside
execute(), which is the first point where Flask is genuinely needed.

The page binds loopback and has no authentication: it runs arbitrary stages as
child processes, which is exactly what the CLI does, on purpose.
"""

from __future__ import annotations

import argparse
import webbrowser
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def add_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """
    Register the web stage's flags on `parser`.

    Kept separate from build_parser() so `mpt`'s subparser and a standalone
    parser share one definition — the CLI owns `--config` at the root level, so
    it is deliberately not registered here.
    """
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        metavar="ADDR",
        help=(
            f"Interface to bind (default: {DEFAULT_HOST}). The page has no "
            "authentication — only change this if you know what you are exposing."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        metavar="N",
        help=f"Port to listen on (default: {DEFAULT_PORT}).",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't open a browser window on start.",
    )
    return parser


EPILOG = """
Examples:
  mpt web                                  # http://127.0.0.1:8765
  mpt web --port 9000
  mpt web --no-browser                     # for a remote SSH tunnel

  # One config somewhere else entirely
  mpt --config /srv/mpt/config.yaml web

Needs the optional extra: `uv sync --extra web`.

The page runs every other stage as a child process and streams its stdout
live. One run at a time; there is no authentication, so keep it on localhost.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mpt web",
        description="Serve a local web interface for every mpt stage.",
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


def execute(args: argparse.Namespace, config_path: Path | None = None) -> int:
    if config_path is None:
        config_path = getattr(args, "config", None)

    try:
        from mpt_autopilot.web.app import create_app
    except ImportError as exc:
        print(f"[ERROR] mpt web needs Flask — run: uv sync --extra web  ({exc})")
        return 1

    from mpt_autopilot import config as shared_config
    from mpt_autopilot.config import ConfigError

    try:
        cfg = shared_config.load(config_path)
    except (ConfigError, FileNotFoundError, OSError) as exc:
        print(f"[ERROR] {exc}")
        return 1

    # host is passed on so the page can answer its own --host (and only that)
    # — see the Host allowlist in app.create_app (W-1).
    app = create_app(cfg, config_path, host=args.host)
    url = f"http://{args.host}:{args.port}/"
    print(f"[web] serving {url} (config: {cfg.path})")
    print("[web] one run at a time, no authentication — Ctrl+C to stop")
    if not args.no_browser:
        webbrowser.open(url)

    try:
        app.run(
            host=args.host,
            port=args.port,
            threaded=True,
            debug=False,
            use_reloader=False,  # a reload would start a second Runner
        )
    except OSError as exc:
        print(f"[ERROR] could not bind {args.host}:{args.port} — {exc}")
        return 1
    return 0


def main() -> None:
    import sys

    from mpt_autopilot.logger import configure_root

    # Give the root logger a handler before any stage runs (M-1). Imported
    # inside main() with sys for the same reason Flask is: this module is
    # imported by build_parser() on every `mpt --help` and must stay light.
    configure_root()
    sys.exit(execute(build_parser().parse_args()))


if __name__ == "__main__":
    main()
