"""
logger.py — file + stdout logger with size-based rotation.

Uses Python's standard logging module with RotatingFileHandler
for reliable multi-file rotation (keeps up to 3 backup files).

Shared across MPT Autopilot stages. `name` keeps one stage's handlers separate
from another's when several stages run in the same process (see `mpt run`).
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_FORMAT = "[%(asctime)s] [%(levelname)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _formatter() -> logging.Formatter:
    return logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)


def configure_root(level: int = logging.INFO) -> None:
    """
    Give the root logger one stderr handler, once per process.

    Thirteen modules hold a `logging.getLogger(__name__)` and nothing ever
    configured a handler for them, so warnings fell through to
    logging.lastResort (unformatted, stderr-only) and info() was dropped
    outright — `refill`'s "skip duplicate" lines went nowhere. Called from every
    entry point (cli.main() and each stage's main()).

    stderr, not stdout: these are diagnostics, and a stage's stdout is its
    user-facing report, which callers pipe and parse.
    """
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_formatter())
    root.addHandler(handler)
    root.setLevel(level)


class Logger:
    """
    Thin wrapper around Python's standard logging that mirrors the
    original info/warn/error interface used throughout the codebase.
    """

    def __init__(self, log_file: Path, max_size: int, name: str = "mpt_autopilot") -> None:
        log_file.parent.mkdir(parents=True, exist_ok=True)

        self._logger = logging.getLogger(name)

        # Already configured for this exact file? Keep the handlers — this is the
        # common case (two modules of one stage each construct a Logger).
        # A different file or size means the caller reconfigured, e.g. `mpt run`
        # calling configure() for the next language: replace the handlers instead
        # of silently keeping the first language's log file, which is what the
        # old `if self._logger.handlers: return` did.
        if self._logger.handlers:
            if self._matches(log_file, max_size):
                return
            self._clear_handlers()

        self._logger.setLevel(logging.DEBUG)

        fmt = _formatter()

        # Rotating file handler — keeps 3 backup files
        fh = RotatingFileHandler(
            log_file,
            maxBytes=max_size,
            backupCount=3,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        fh.setLevel(logging.DEBUG)
        self._logger.addHandler(fh)

        # Stdout for INFO/WARN, stderr for ERROR
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        sh.setLevel(logging.DEBUG)
        sh.addFilter(_BelowError())
        self._logger.addHandler(sh)

        err_h = logging.StreamHandler(sys.stderr)
        err_h.setFormatter(fmt)
        err_h.setLevel(logging.ERROR)
        self._logger.addHandler(err_h)

    def _matches(self, log_file: Path, max_size: int) -> bool:
        """True when an existing RotatingFileHandler already targets this file and size."""
        for handler in self._logger.handlers:
            if isinstance(handler, RotatingFileHandler):
                return (
                    Path(handler.baseFilename) == log_file.resolve()
                    and handler.maxBytes == max_size
                )
        return False

    def _clear_handlers(self) -> None:
        for handler in list(self._logger.handlers):
            self._logger.removeHandler(handler)
            handler.close()

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._logger.info(msg, *args, **kwargs)

    def warn(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._logger.warning(msg, *args, **kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._logger.warning(msg, *args, **kwargs)

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._logger.error(msg, *args, **kwargs)


class _BelowError(logging.Filter):
    """Pass only records below ERROR level (for stdout handler)."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < logging.ERROR
