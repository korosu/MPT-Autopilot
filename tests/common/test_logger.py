"""Tests for logger.py — the RotatingFileHandler behaviour (part 11).

Logger's reconfiguration and configure_root are covered in
test_shared_audit_fixes.py; this file covers what nothing did: actual
rotation to the three backup files that distinguishes the shared Logger
from the one-backup hand-rolled loop batch/run.py used to have.
"""

from __future__ import annotations

import logging
from pathlib import Path

from mpt_autopilot.logger import Logger


def _close(name: str) -> None:
    lg = logging.getLogger(name)
    for handler in list(lg.handlers):
        lg.removeHandler(handler)
        handler.close()


def test_logger_rotates_into_backup_files(tmp_path: Path):
    log_file = tmp_path / "b.log"
    name = "mpt_autopilot.test.rotate"
    try:
        lg = Logger(log_file, max_size=200, name=name)
        for i in range(60):
            lg.info(f"line {i} " + "x" * 30)
        assert log_file.exists()
        assert (tmp_path / "b.log.1").exists()
    finally:
        _close(name)


def test_logger_keeps_at_most_three_backups(tmp_path: Path):
    name = "mpt_autopilot.test.rotate2"
    try:
        lg = Logger(tmp_path / "b.log", max_size=150, name=name)
        for i in range(300):
            lg.info(f"line {i} " + "y" * 40)
        backups = sorted(p.name for p in tmp_path.glob("b.log.*"))
        assert backups == ["b.log.1", "b.log.2", "b.log.3"]
    finally:
        _close(name)


def test_logger_latest_file_stays_under_the_cap(tmp_path: Path):
    log_file = tmp_path / "b.log"
    name = "mpt_autopilot.test.rotate3"
    try:
        lg = Logger(log_file, max_size=150, name=name)
        for i in range(100):
            lg.info(f"line {i} " + "z" * 20)
        assert log_file.stat().st_size <= 150 + 200  # current line headroom
    finally:
        _close(name)


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
