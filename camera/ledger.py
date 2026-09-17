"""Camera import dedup ledger.

Dedup key: CCAPI content ID — NOT filename. Canon resets IMG_0001 after card
format; filename-only dedup silently skips re-imported photos.

Three consecutive size-verify failures → file is skipped and excluded from
future drains until manually cleared (ledger.skip). Prevents corrupt-on-camera
files from spinning the poll loop indefinitely.
"""

import sqlite3
import time

MAX_FAILS = 3  # strike limit before a file is permanently skipped

_SQL_PULLS = """
CREATE TABLE IF NOT EXISTS pulls (
    content_id  TEXT    PRIMARY KEY,
    dest_path   TEXT    NOT NULL,
    size        INTEGER NOT NULL,
    pulled_at   REAL    NOT NULL
)
"""

_SQL_FAILURES = """
CREATE TABLE IF NOT EXISTS failures (
    content_id  TEXT    PRIMARY KEY,
    fail_count  INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0
)
"""


class Ledger:
    """Thread-safe (WAL mode) SQLite ledger for camera import dedup."""

    def __init__(self, db_path):
        assert db_path, "db_path required"
        self._path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SQL_PULLS)
        self._conn.execute(_SQL_FAILURES)
        self._conn.commit()

    def is_pulled(self, content_id):
        """True if content_id has been successfully pulled and verified."""
        assert content_id, "content_id required"
        row = self._conn.execute(
            "SELECT 1 FROM pulls WHERE content_id = ?", (content_id,)
        ).fetchone()
        return row is not None

    def mark_pulled(self, content_id, dest_path, size, ts=None):
        """Record a successfully size-verified pull. Idempotent."""
        assert content_id, "content_id required"
        assert dest_path, "dest_path required"
        assert isinstance(size, int) and size >= 0, "size must be non-negative int"
        if ts is None:
            ts = time.time()
        self._conn.execute(
            "INSERT OR REPLACE INTO pulls (content_id, dest_path, size, pulled_at)"
            " VALUES (?, ?, ?, ?)",
            (content_id, dest_path, size, float(ts)),
        )
        self._conn.commit()

    def increment_fail(self, content_id):
        """Record one failed size-verify. Returns new consecutive fail count."""
        assert content_id, "content_id required"
        self._conn.execute(
            "INSERT INTO failures (content_id, fail_count, skipped) VALUES (?, 1, 0)"
            " ON CONFLICT(content_id) DO UPDATE SET fail_count = fail_count + 1",
            (content_id,),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT fail_count FROM failures WHERE content_id = ?", (content_id,)
        ).fetchone()
        assert row is not None, "failure row must exist after insert"
        return row[0]

    def fail_count(self, content_id):
        """Current consecutive-failure count (0 if never failed)."""
        assert content_id, "content_id required"
        row = self._conn.execute(
            "SELECT fail_count FROM failures WHERE content_id = ?", (content_id,)
        ).fetchone()
        return row[0] if row else 0

    def skip(self, content_id):
        """Permanently skip content_id. Excluded from future drains."""
        assert content_id, "content_id required"
        self._conn.execute(
            "INSERT INTO failures (content_id, fail_count, skipped) VALUES (?, 0, 1)"
            " ON CONFLICT(content_id) DO UPDATE SET skipped = 1",
            (content_id,),
        )
        self._conn.commit()

    def is_skipped(self, content_id):
        """True if content_id has been permanently skipped."""
        assert content_id, "content_id required"
        row = self._conn.execute(
            "SELECT skipped FROM failures WHERE content_id = ?", (content_id,)
        ).fetchone()
        return bool(row and row[0])

    def close(self):
        """Close the database connection."""
        self._conn.close()
