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


CONTENT_EXTS = frozenset({".pdf", ".md", ".markdown", ".txt"})
CONTENT_CHAR_CAP = 40000            # per-file extracted-text cap
CONTENT_FILE_MAX = 20_000_000       # skip files larger than this before extracting


def _extract_text(abspath, ext):
    """Best-effort text extraction for a doc file. Never raises; capped to CONTENT_CHAR_CAP."""
    assert isinstance(abspath, str) and abspath, "abspath required"
    assert isinstance(ext, str), "ext must be str"
    try:
        if ext == ".pdf":
            import pymupdf
            parts, n = [], 0
            doc = pymupdf.open(abspath)
            try:
                for page in doc:                       # bounded by CONTENT_CHAR_CAP break
                    t = page.get_text() or ""
                    parts.append(t)
                    n += len(t)
                    if n >= CONTENT_CHAR_CAP:
                        break
            finally:
                doc.close()
            return "".join(parts)[:CONTENT_CHAR_CAP]
        with open(abspath, "r", errors="ignore") as fh:
            return fh.read(CONTENT_CHAR_CAP)
    except Exception:
        return ""


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
        con.execute("CREATE VIRTUAL TABLE content_fts USING fts5(path UNINDEXED, body)")
        batch = []
        cbatch = []                                           # extracted doc bodies
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
                rel = os.path.relpath(full, root)
                batch.append((fn, rel, kind_for(fn), st.st_size, int(st.st_mtime)))
                count += 1
                ext = os.path.splitext(fn)[1].lower()
                if ext in CONTENT_EXTS and st.st_size <= CONTENT_FILE_MAX:
                    body = _extract_text(full, ext)          # never raises
                    if body:
                        cbatch.append((rel, body))
                        if len(cbatch) >= 50:                # throttle the heavy step
                            con.executemany("INSERT INTO content_fts(path,body) VALUES(?,?)", cbatch)
                            con.commit()
                            cbatch = []
                            time.sleep(_THROTTLE)
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
        if cbatch:
            con.executemany("INSERT INTO content_fts(path,body) VALUES(?,?)", cbatch)
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
            if segs and t == segs[0]:          # matched a TOP-LEVEL folder — strong signal
                s += 1.5
            if in_name:                        # matched the basename itself
                s += 1.0
        for seg in segs:                       # multiple query terms in ONE path
            if sum(1 for t in terms if t in seg) >= 2:   # component (e.g. "ares-dashboard")
                s += 3.0                                  # = a very strong match
                break
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


def content_search(query, limit=6, db=INDEX_DB):
    """Full-text search INSIDE extracted document bodies (pdf/md/txt). Returns
    [{path, kind, snippet}] with the matching passage. Empty if the content
    table doesn't exist yet (old index) or nothing matches."""
    assert isinstance(query, str), "query must be str"
    assert isinstance(limit, int) and limit > 0, "limit must be positive int"
    if not query.strip() or not os.path.exists(db):
        return []
    terms = [t for t in re.findall(r"[A-Za-z0-9_]+", query.lower())
             if len(t) > 1 and t not in STOPWORDS]
    if not terms:
        return []
    match = " OR ".join(t + "*" for t in terms)
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT path, snippet(content_fts, 1, '[', ']', ' … ', 14) "
            "FROM content_fts WHERE content_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, limit)).fetchall()
    except sqlite3.OperationalError:                          # no content_fts yet
        return []
    finally:
        con.close()
    return [{"path": r[0], "kind": kind_for(os.path.basename(r[0])),
             "snippet": " ".join((r[1] or "").split())} for r in rows]


# ── Semantic search (Phase 3): doc-chunk embeddings via Ollama + numpy cosine ──
# numpy imported lazily inside functions so a missing dep never breaks import.
EMB_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
EMB_FILE = os.path.join(_APP_ROOT, "ai_data", "doc_embeddings.npy")
CHUNKS_FILE = os.path.join(_APP_ROOT, "ai_data", "doc_chunks.json")
CHUNK_SIZE = 500
CHUNK_OVERLAP = 80
EMB_BATCH = 64
EMB_CHUNK_CAP = 200_000        # hard upper bound on total chunks (Power of Ten rule 2)


def _chunk(text):
    assert isinstance(text, str), "text must be str"
    step = CHUNK_SIZE - CHUNK_OVERLAP
    assert step > 0, "overlap must be < size"
    out, i, n = [], 0, len(text)
    while i < n:                                   # bounded: i advances by step each pass
        piece = text[i:i + CHUNK_SIZE].strip()
        if piece:
            out.append(piece)
        i += step
    return out


def _embed(texts):
    """Embed a list of strings via Ollama /api/embed (stdlib urllib). -> list[vec]."""
    assert isinstance(texts, list) and texts, "texts must be non-empty list"
    import json as _json, urllib.request as _u
    host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    body = _json.dumps({"model": EMB_MODEL, "input": texts}).encode()
    req = _u.Request(host + "/api/embed", data=body,
                     headers={"Content-Type": "application/json"})
    with _u.urlopen(req, timeout=300) as r:
        return _json.loads(r.read()).get("embeddings", [])


def embed_docs(db=INDEX_DB):
    """Embed all content_fts doc chunks → EMB_FILE + CHUNKS_FILE. Returns chunk
    count. Best-effort: any failure leaves prior files intact and returns 0."""
    assert isinstance(db, str) and db, "db required"
    if not os.path.exists(db):
        return 0
    # Never attempt a full embed while the GPU is on loan (VM200 gaming): CPU
    # embedding is ~1000x slower and would wedge the reindex thread for hours.
    if os.path.exists(os.path.join(_APP_ROOT, ".gpu-on-loan")):
        return 0
    import json as _json
    import numpy as _np
    con = sqlite3.connect(db)
    try:
        rows = con.execute("SELECT path, body FROM content_fts").fetchall()
    except sqlite3.OperationalError:
        return 0
    finally:
        con.close()
    chunks = []
    for path, body in rows:
        for piece in _chunk(body or ""):
            chunks.append({"path": path, "chunk": piece})
        if len(chunks) >= EMB_CHUNK_CAP:
            break
    if not chunks:
        return 0
    try:
        vecs = []
        for i in range(0, len(chunks), EMB_BATCH):
            vecs.extend(_embed([c["chunk"] for c in chunks[i:i + EMB_BATCH]]))
            time.sleep(_THROTTLE)
        mat = _np.asarray(vecs, dtype="float32")
        assert mat.shape[0] == len(chunks), "embed count mismatch"
        norms = _np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        mat = mat / norms
        os.makedirs(os.path.dirname(EMB_FILE), exist_ok=True)
        _np.save(EMB_FILE + ".tmp.npy", mat)
        os.replace(EMB_FILE + ".tmp.npy", EMB_FILE)
        with open(CHUNKS_FILE + ".tmp", "w") as fh:
            _json.dump(chunks, fh)
        os.replace(CHUNKS_FILE + ".tmp", CHUNKS_FILE)
    except Exception:
        return 0
    return len(chunks)


def semantic_search(query, k=8, db=INDEX_DB):
    """Cosine top-k over doc-chunk embeddings, deduped by path. -> [{path,chunk,score}]."""
    assert isinstance(query, str), "query must be str"
    assert isinstance(k, int) and k > 0, "k must be positive int"
    if not query.strip() or not (os.path.exists(EMB_FILE) and os.path.exists(CHUNKS_FILE)):
        return []
    import json as _json
    import numpy as _np
    try:
        qv = _embed([query])
        if not qv:
            return []
        q = _np.asarray(qv[0], dtype="float32")
        q = q / (_np.linalg.norm(q) or 1.0)
        mat = _np.load(EMB_FILE, mmap_mode="r")
        chunks = _json.load(open(CHUNKS_FILE))
    except Exception:
        return []
    if mat.shape[0] != len(chunks):
        return []
    sims = _np.asarray(mat @ q)
    order = _np.argsort(-sims)[:k * 4]             # oversample, dedupe by path below
    out, seen = [], set()
    for idx in order:
        c = chunks[int(idx)]
        if c["path"] in seen:
            continue
        seen.add(c["path"])
        out.append({"path": c["path"], "chunk": c["chunk"], "score": float(sims[int(idx)])})
        if len(out) >= k:
            break
    return out


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
            try:
                embed_docs()                       # best-effort semantic index (Phase 3)
            except Exception:
                pass
        except Exception as e:                              # pragma: no cover
            _state["error"] = str(e)
        finally:
            _state["building"] = False
            _build_lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return True
