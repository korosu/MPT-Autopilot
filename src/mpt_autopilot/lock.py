"""
generator/lock.py

Minimal cross-platform advisory file lock. Used to stop two concurrent
`refill` / `init-seen` runs from interleaving writes to the same
jobs_<lang>.yaml or seen*.txt file. Implemented with atomic exclusive
file creation so it behaves the same on Linux, macOS, and Windows
without any extra dependencies.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

_STALE_AFTER = 60      # seconds — a lock file older than this is assumed abandoned
_WAIT_TIMEOUT = 30     # seconds — how long to wait for another process to finish
_POLL_INTERVAL = 0.2   # seconds


@contextmanager
def file_lock(target: Path, timeout: float = _WAIT_TIMEOUT):
    """
    Hold an exclusive lock for `target` while the block runs, by atomically
    creating a `<target>.lock` marker file. Waits up to `timeout` seconds
    for a concurrent holder to release it; a lock older than _STALE_AFTER
    is treated as abandoned (e.g. a previous run that crashed) and removed.

    If the lock still can't be acquired after `timeout`, proceeds anyway
    (a missed lock is safer here than blocking the CLI forever) and just
    warns — writes are append-only, so the worst case is a duplicate line,
    which the reader side already de-duplicates.
    """
    lock_path = Path(str(target) + ".lock")
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                acquired = True
                break
            except FileExistsError:
                try:
                    age = time.time() - lock_path.stat().st_mtime
                except FileNotFoundError:
                    continue  # lock was released between our check and stat(); retry
                if age > _STALE_AFTER:
                    lock_path.unlink(missing_ok=True)
                    continue
                if time.monotonic() >= deadline:
                    print(f"  [warn] could not acquire lock on {target.name} "
                          f"after {timeout:.0f}s — proceeding without it")
                    break
                time.sleep(_POLL_INTERVAL)
        yield
    finally:
        if acquired:
            lock_path.unlink(missing_ok=True)
