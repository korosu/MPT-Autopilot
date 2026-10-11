"""
lock.py

Minimal cross-platform advisory file lock. Used to stop two concurrent
`mpt refill` / `mpt init-seen` / `mpt batch` runs from interleaving writes to
the same jobs_<lang>.yaml or seen*.txt file. Implemented with atomic exclusive
file creation so it behaves the same on Linux, macOS, and Windows
without any extra dependencies.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path

_logger = logging.getLogger(__name__)

_STALE_AFTER = 60  # seconds — a lock file older than this is assumed abandoned
_WAIT_TIMEOUT = 30  # seconds — how long to wait for another process to finish
_POLL_INTERVAL = 0.2  # seconds


def _steal_stale(lock_path: Path) -> None:
    """
    Remove an abandoned lock so exactly one waiter can take it next.

    Renaming instead of unlinking is what makes this safe: when two processes
    both see an expired lock, os.rename() succeeds for the one that gets there
    first and raises FileNotFoundError for the other, which then loops back to
    the O_EXCL create. A plain unlink() lets both proceed — the second one
    deletes the fresh lock the first had already created, and both run believing
    they hold it.
    """
    stolen = lock_path.with_name(f"{lock_path.name}.stale.{os.getpid()}")
    try:
        os.rename(lock_path, stolen)
    except (FileNotFoundError, PermissionError):
        # Lost the race (or Windows has the file open elsewhere): retry the
        # normal acquisition path.
        return
    stolen.unlink(missing_ok=True)


def lock_path_for(target: Path) -> Path:
    """
    The marker file file_lock() creates for `target`.

    Callers that hold a file_lock for a long time (the batch run lock) touch
    this file's mtime as a keepalive so a concurrent process does not mistake
    the live lock for an abandoned one; they need the exact path, without
    re-deriving file_lock's internal naming scheme.
    """
    return Path(str(target) + ".lock")


@contextmanager
def file_lock(
    target: Path,
    timeout: float = _WAIT_TIMEOUT,
    strict: bool = False,
    stale_after: float | None = None,
):
    """
    Hold an exclusive lock for `target` while the block runs, by atomically
    creating a `<target>.lock` marker file. Waits up to `timeout` seconds
    for a concurrent holder to release it; a lock older than `stale_after`
    (default `_STALE_AFTER`) is treated as abandoned (e.g. a previous run that
    crashed) and removed.

    Not re-entrant: a nested file_lock() on the same target blocks until
    `timeout` and then proceeds unlocked (or raises, with strict=True).

    `stale_after` is a parameter because the right value depends on how long the
    protected work can legitimately take: seconds for a seen.txt append, up to
    half an hour for a batch run polling the MPT API. It defaults to None rather
    than to _STALE_AFTER directly so the module constant is read per call, which
    keeps it patchable.

    If the lock still can't be acquired after `timeout`:
      - strict=False (default; used by seen.py): proceeds anyway and just
        warns. Safe there because seen.py's writes are single-line and
        idempotent — the worst case of two racing writers is a duplicate
        line, which the reader side already de-duplicates.
    """
    expiry = _STALE_AFTER if stale_after is None else stale_after
    lock_path = lock_path_for(Path(target))
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
                if age > expiry:
                    _steal_stale(lock_path)
                    continue
                if time.monotonic() >= deadline:
                    if strict:
                        raise TimeoutError(
                            f"Could not acquire lock on {target.name} after "
                            f"{timeout:.0f}s — another process appears to be "
                            f"writing it. Refusing to proceed without the "
                            f"lock (this write can't safely be retried "
                            f"unlocked). Try again once the other process "
                            f"finishes."
                        )
                    _logger.warning(
                        "could not acquire lock on %s after %.0fs — proceeding without it",
                        target.name,
                        timeout,
                    )
                    break
                time.sleep(_POLL_INTERVAL)
        yield
    finally:
        if acquired:
            lock_path.unlink(missing_ok=True)
