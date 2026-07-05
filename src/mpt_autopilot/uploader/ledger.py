from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def open_ledger(path: Path) -> sqlite3.Connection:
    """Open or create the ledger database, ensuring parent directory exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS uploads (
            account TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            video_name TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (account, content_hash)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_uploads_status ON uploads(account, status)"
    )
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
    """Insert or update to status 'started', but never regress from uploaded/moved."""
    # Insert if new; if exists, only update back to started if currently started
    conn.execute(
        "INSERT OR IGNORE INTO uploads (account, content_hash, video_name, status, created_at) "
        "VALUES (?, ?, ?, 'started', ?)",
        (account, content_hash, video_name, datetime.now(timezone.utc).isoformat()),
    )
    conn.execute(
        "UPDATE uploads SET video_name = ?, created_at = ? "
        "WHERE account = ? AND content_hash = ? AND status = 'started'",
        (video_name, datetime.now(timezone.utc).isoformat(), account, content_hash),
    )
    conn.commit()


def mark_uploaded(conn: sqlite3.Connection, account: str, content_hash: str) -> None:
    """Update status to 'uploaded'."""
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