"""File locator index (Phase 1 of the file-explorer assistant).

A throttled full-pool walk (vault + dotfiles excluded) into a standalone SQLite
FTS5 table, queried by the /api/files/ask locator. Read-only w.r.t. the browsed
tree — the only write is the index DB itself. The walk is bounded, batched, and
sleeps between batches so it can never wedge the box (unlike an ad-hoc grep).
"""
import os
import re
import time
import sqlite3
import threading

from system.files_api import ROOT, DENY_NAMES, kind_for

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_DB = os.path.join(_APP_ROOT, "ai_data", "files_index.db")

WALK_CAP = 1_500_000        # hard upper bound on indexed files (Power of Ten rule 2)
_BATCH = 500
_THROTTLE = 0.02            # seconds slept per batch — keeps the walk gentle

# Dependency/cache dirs — never user content, and they dwarf the real files
# (a pool of git repos hits the 500k cap on node_modules/site-packages alone).
# Dotdirs (.git, .venv, .cache, .npm, .cargo, .tox, ...) are already excluded
# by the leading-dot rule below.
JUNK_DIRS = frozenset({
    "node_modules", "__pycache__", "site-packages", "venv", "env",
    "bower_components", ".pytest_cache",
})

# Backup/mirror trees — redundant copies that bury real files with duplicates
# (HERMES-SIDEKICK is a full mirror of the other box). Dir-only: never applied
# to filenames, so a file literally named "backup.txt" is still indexed.
BACKUP_DIRS = frozenset({
    "HERMES-SIDEKICK", "NEXUS-SIDEKICK", "PROMETHEUS_BACKUP", "BACKUP",
})

_state = {"building": False, "count": 0, "built_at": 0, "error": None}
_build_lock = threading.Lock()


def _excluded(name):
    """File/dir exclusion: hidden dotfiles + vault + dependency dirs."""
    assert isinstance(name, str), "name must be str"
    return name.startswith(".") or name in DENY_NAMES or name in JUNK_DIRS


def _skip_dir(name):
    """Extra dir-only pruning: backup/mirror trees on top of _excluded."""
    return _excluded(name) or name in BACKUP_DIRS


def build_index(root=ROOT, db=INDEX_DB):
    """Rebuild the FTS index from scratch. Bounded + throttled. Returns count."""
    assert isinstance(root, str) and root, "root required"
    assert isinstance(db, str) and db, "db path required"
    os.makedirs(os.path.dirname(db), exist_ok=True)
    tmp = db + ".building"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    count = 0
    try:
        con.execute("CREATE VIRTUAL TABLE files_fts USING fts5("
                    "name, path, kind UNINDEXED, size UNINDEXED, mtime UNINDEXED)")
        batch = []
        dirs = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not _skip_dir(d)]   # prune, don't descend
            for d in dirnames:                                        # index folders too
                if count >= WALK_CAP:
                    break
                dp = os.path.join(dirpath, d)
                try:
                    dst = os.stat(dp, follow_symlinks=False)
                except OSError:
                    continue
                batch.append((d, os.path.relpath(dp, root), "dir", 0, int(dst.st_mtime)))
                count += 1
            for fn in filenames:
                if _excluded(fn) or count >= WALK_CAP:
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    st = os.stat(full, follow_symlinks=False)
                except OSError:
                    continue
                batch.append((fn, os.path.relpath(full, root), kind_for(fn),
                              st.st_size, int(st.st_mtime)))
                count += 1
                if len(batch) >= _BATCH:
                    con.executemany("INSERT INTO files_fts(name,path,kind,size,mtime) "
                                    "VALUES(?,?,?,?,?)", batch)
                    con.commit()
                    batch = []
                    time.sleep(_THROTTLE)
            dirs += 1
            if dirs % 50 == 0:
                time.sleep(_THROTTLE)
            if count >= WALK_CAP:
                break
        if batch:
            con.executemany("INSERT INTO files_fts(name,path,kind,size,mtime) "
                            "VALUES(?,?,?,?,?)", batch)
            con.commit()
    finally:
        con.close()
    os.replace(tmp, db)                                    # atomic swap
    return count


# Question/filler words that must not drive filename matching — otherwise
# "which resume is the newest" matches which.py, datetimes.json, etc.
STOPWORDS = frozenset((
    "a an the is are was were be of to in on at for and or my me i you it this "
    "that these those which what where when who whom how why do does did show find "
    "get give tell me my our us list all any some most more newest latest recent "
    "oldest last first up down date dated updated current version file files folder "
    "directory dir document documents doc docs thing stuff about have has need want"
).split())

# Recency intent → caller should sort by mtime instead of FTS relevance.
RECENCY_WORDS = ("newest", "latest", "recent", "most recent", "up to date",
                 "up-to-date", "up date", "current", "last modified")


def recency_intent(query):
    q = query.lower()
    return any(w in q for w in RECENCY_WORDS)


def search(query, limit=40, db=INDEX_DB):
    """FTS name/path search. Returns [{name,path,kind,size,mtime}], best-ranked
    first (or newest-first when the query implies recency)."""
    assert isinstance(query, str), "query must be str"
    assert isinstance(limit, int) and limit > 0, "limit must be positive int"
    if not query.strip() or not os.path.exists(db):
        return []
    terms = [t for t in re.findall(r"[A-Za-z0-9_]+", query.lower())
             if len(t) > 1 and t not in STOPWORDS]
    if not terms:
        return []
    match = " OR ".join(t + "*" for t in terms)            # prefix, any-term
    con = sqlite3.connect(db)
    try:
        # bm25 (ORDER BY rank) floats rare-term matches into the top pool; we then
        # re-rank that pool with a heuristic that rewards exact folder/name-component
        # hits and shallow paths, and demotes deep vendored trees.
        rows = con.execute(
            "SELECT name,path,kind,size,mtime FROM files_fts WHERE files_fts MATCH ? "
            "ORDER BY rank LIMIT 300", (match,)).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()

    def _score(r):
        name = r[0].lower()
        path = r[1].lower()
        segs = [s for s in path.split("/") if s]
        s = 0.0
        for t in terms:
            in_name, in_path = t in name, t in path
            if in_name or in_path:
                s += 1.0
            if t in segs:                      # matched a whole folder/file name
                s += 2.5
            if in_name:                        # matched the basename itself
                s += 1.0
        s -= 0.15 * max(0, len(segs) - 1)      # shallower = more likely what you want
        if r[2] == "dir":                      # a matching folder answers "where is X"
            s += 0.6
        return s

    rows = sorted(rows, key=_score, reverse=True)[:limit]
    results = [{"name": r[0], "path": r[1], "kind": r[2], "size": r[3], "mtime": r[4]}
               for r in rows]
    if recency_intent(query):
        results.sort(key=lambda c: c["mtime"], reverse=True)   # newest first
    return results


def index_status(db=INDEX_DB):
    """Return build state; fills count from the DB if idle and present."""
    st = dict(_state)
    if not st["building"] and os.path.exists(db):
        con = sqlite3.connect(db)
        try:
            st["count"] = con.execute("SELECT count(*) FROM files_fts").fetchone()[0]
        except sqlite3.OperationalError:
            pass
        finally:
            con.close()
    st["exists"] = os.path.exists(db)
    return st


def start_reindex():
    """Kick a background rebuild unless one is running. Returns True if started."""
    if not _build_lock.acquire(blocking=False):
        return False

    def _run():
        _state.update(building=True, error=None)
        try:
            n = build_index()
            _state.update(count=n, built_at=int(time.time()))
        except Exception as e:                              # pragma: no cover
            _state["error"] = str(e)
        finally:
            _state["building"] = False
            _build_lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return True
