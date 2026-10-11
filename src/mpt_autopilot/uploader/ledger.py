"""
ledger.py — SQLite-based crash-safe ledger for upload dedup.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_logger = logging.getLogger(__name__)


def open_ledger(path: Path) -> sqlite3.Connection:
    """
    Open or create the ledger database, ensuring parent directory exists.

    Both `mpt upload` and `mpt run` (which calls the uploader stage) write to
    this database, and a user can run them at the same time. SQLite's defaults
    are a rollback journal with a 5-second busy timeout, so two concurrent
    writers raise an unhandled `sqlite3.OperationalError: database is locked`.

    WAL lets readers and writers proceed in parallel, and the 30s timeout means
    a busy writer waits for the other run's transaction instead of aborting.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    # WAL silently falls back to a rollback journal on filesystems that don't
    # support it (network shares). The parallel-runs guarantee above would
    # then evaporate with no trace, so verify instead of hoping (audit L).
    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
    if mode is None or str(mode[0]).lower() != "wal":
        _logger.warning(
            "ledger %s: WAL mode unavailable (journal_mode=%r) — concurrent "
            "runs may fail with 'database is locked'",
            path,
            mode[0] if mode else None,
        )
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS uploads (
            account TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            video_name TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            video_id TEXT,
            PRIMARY KEY (account, content_hash)
        )
        """
    )
    # R8: CREATE TABLE IF NOT EXISTS never teaches an EXISTING database the
    # new column — an explicit migration is the only way ledgers created
    # before video_id keep working instead of failing every UPDATE that
    # mentions it.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(uploads)")}
    if "video_id" not in columns:
        conn.execute("ALTER TABLE uploads ADD COLUMN video_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_uploads_status ON uploads(account, status)")
    conn.commit()
    return conn


def sha256_file(path: Path) -> str:
    """Return SHA-256 hex digest of file, streamed in 1 MiB chunks."""
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def is_done(conn: sqlite3.Connection, account: str, content_hash: str) -> bool:
    """Return True if status is 'uploaded' or 'moved'."""
    cur = conn.execute(
        "SELECT status FROM uploads WHERE account = ? AND content_hash = ?",
        (account, content_hash),
    )
    row = cur.fetchone()
    if row is None:
        return False
    return row[0] in ("uploaded", "moved")


def mark_started(
    conn: sqlite3.Connection, account: str, content_hash: str, video_name: str
) -> None:
    """
    Insert or update to status 'started', but never regress from uploaded/moved.

    created_at is set only by the INSERT: a second mark_started for the same
    upload (a retried run after a crash) used to overwrite it, destroying the
    record of when the upload actually began (audit L).
    """
    conn.execute(
        "INSERT OR IGNORE INTO uploads (account, content_hash, video_name, status, created_at) "
        "VALUES (?, ?, ?, 'started', ?)",
        (account, content_hash, video_name, datetime.now(timezone.utc).isoformat()),
    )
    conn.execute(
        "UPDATE uploads SET video_name = ? "
        "WHERE account = ? AND content_hash = ? AND status = 'started'",
        (video_name, account, content_hash),
    )
    conn.commit()


def mark_uploaded(
    conn: sqlite3.Connection, account: str, content_hash: str, video_id: str | None = None
) -> None:
    """
    Update status to 'uploaded'.

    video_id (R8) is persisted when the uploader could parse the YouTube id
    from youtubeuploader's output — the missing link of the file→video
    mapping; None leaves any previously stored value untouched.
    """
    if video_id:
        conn.execute(
            "UPDATE uploads SET status = 'uploaded', video_id = ? "
            "WHERE account = ? AND content_hash = ?",
            (video_id, account, content_hash),
        )
    else:
        conn.execute(
            "UPDATE uploads SET status = 'uploaded' WHERE account = ? AND content_hash = ?",
            (account, content_hash),
        )
    conn.commit()


def mark_moved(conn: sqlite3.Connection, account: str, content_hash: str) -> None:
    """Update status to 'moved'."""
    conn.execute(
        "UPDATE uploads SET status = 'moved' WHERE account = ? AND content_hash = ?",
        (account, content_hash),
    )
    conn.commit()
