"""
logger.py — simple file + stdout logger with size-based rotation.
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


class Logger:
    def __init__(self, log_file: Path, max_size: int) -> None:
        self._log_file = log_file
        self._max_size = max_size
        log_file.parent.mkdir(parents=True, exist_ok=True)

    def _now(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def _rotate_if_needed(self) -> None:
        """Move the current log to .1.log when it exceeds the size limit."""
        if self._log_file.exists() and self._log_file.stat().st_size > self._max_size:
            rotated = self._log_file.with_suffix(".1.log")
            shutil.move(str(self._log_file), str(rotated))

    def _write(self, level: str, msg: str) -> None:
        line = f"[{self._now()}] [{level}] {msg}"
        print(line, file=sys.stdout if level != "ERROR" else sys.stderr)
        self._rotate_if_needed()
        with open(self._log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def info(self, msg: str) -> None:
        self._write("INFO", msg)

    def warn(self, msg: str) -> None:
        self._write("WARN", msg)

    def error(self, msg: str) -> None:
        self._write("ERROR", msg)

