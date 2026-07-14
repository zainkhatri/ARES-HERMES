"""SQLite-backed photo index — the ONLY sanctioned reader/writer.

Replaces photo_index.json (archived as photo_index.pre-sqlite.json after the
JSON file was corrupted/clobbered six times by one-off scripts). Import this
module and use load_items()/save_items(); never write photo_index.db with
ad-hoc SQL from one-off scripts — route every write through save_items so the
shrink guard can veto a buggy caller.

CLI:
  python3 photo_db.py migrate [src.json]   one-time import from a JSON index
  python3 photo_db.py export  [dst.json]   JSON snapshot (backup / debugging)
  python3 photo_db.py count
"""
import contextlib
import json
import os
import sqlite3

_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_DIR, "photo_index.db")


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS photos (path TEXT PRIMARY KEY, item TEXT NOT NULL)"
    )
    return conn


def version():
    """Cheap change stamp for cache invalidation. WAL commits touch the -wal
    file, checkpoints touch the main db — the max mtime of both moves on every
    write, including writes from other processes (scanner, indexer)."""
    v = 0.0
    for p in (DB_PATH, DB_PATH + "-wal"):
        try:
            v = max(v, os.path.getmtime(p))
        except OSError:
            pass
    return v


def load_items():
    """Return every index item as a list of dicts (unordered)."""
    with contextlib.closing(_connect()) as conn:
        rows = conn.execute("SELECT item FROM photos").fetchall()
    return [json.loads(r[0]) for r in rows]


def save_items(items, force=False):
    """Replace the whole index in one transaction.

    Refuses a >50% shrink unless force=True — the guard that would have caught
    the Jun 2026 dedup run that clobbered 46,906 items down to 5. Duplicate
    paths raise IntegrityError and roll the whole write back.
    """
    assert isinstance(items, list), "photo index must be a list"
    assert all(isinstance(it, dict) and it.get("path") for it in items), (
        "every index item must be a dict with a non-empty path"
    )
    with contextlib.closing(_connect()) as conn:
        prev = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
        assert force or not (prev >= 100 and len(items) < prev * 0.5), (
            f"save_items refused: {len(items)} items would shrink the index "
            f"from {prev} (>50% drop) — pass force=True if this is intentional"
        )
        with conn:  # one transaction: commit on success, rollback on error
            conn.execute("DELETE FROM photos")
            conn.executemany(
                "INSERT INTO photos (path, item) VALUES (?, ?)",
                ((it["path"], json.dumps(it)) for it in items),
            )


def _cli(argv):
    cmd = argv[1] if len(argv) > 1 else "count"
    if cmd == "migrate":
        src = argv[2] if len(argv) > 2 else os.path.join(_DIR, "photo_index.json")
        with open(src) as f:
            items = json.load(f)
        save_items(items, force=True)
        print(f"migrated {len(items)} items: {src} -> {DB_PATH}")
    elif cmd == "export":
        dst = argv[2] if len(argv) > 2 else os.path.join(_DIR, "photo_index.export.json")
        items = load_items()
        with open(dst, "w") as f:
            json.dump(items, f)
        print(f"exported {len(items)} items -> {dst}")
    elif cmd == "count":
        print(len(load_items()))
    else:
        print(__doc__)


if __name__ == "__main__":
    import sys
    _cli(sys.argv)
