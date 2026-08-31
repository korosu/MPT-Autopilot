"""
logger.py — file + stdout logger with size-based rotation.

Uses Python's standard logging module with RotatingFileHandler
for reliable multi-file rotation (keeps up to 3 backup files).
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


class Logger:
    """
    Thin wrapper around Python's standard logging that mirrors the
    original info/warn/error interface used throughout the codebase.
    """

    def __init__(self, log_file: Path, max_size: int) -> None:
        log_file.parent.mkdir(parents=True, exist_ok=True)

        self._logger = logging.getLogger("hashtag_enricher")

        # Guard against duplicate handlers when instantiated multiple times
        if self._logger.handlers:
            return

        self._logger.setLevel(logging.DEBUG)

        fmt = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

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

    def info(self, msg: str) -> None:
        self._logger.info(msg)

    def warn(self, msg: str) -> None:
        self._logger.warning(msg)

    def error(self, msg: str) -> None:
        self._logger.error(msg)


class _BelowError(logging.Filter):
    """Pass only records below ERROR level (for stdout handler)."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < logging.ERROR
