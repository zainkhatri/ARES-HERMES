#!/usr/bin/env python3
"""
ARES Photo Gallery Deduplicator
Pass 1: SHA-256 exact byte duplicates
Pass 2: CLIP near-duplicates (cosine sim >= 0.995)

Run inside LXC 101:
  pct exec 101 -- /mnt/data/ares-app/.venv/bin/python /mnt/data/ares-app/dedup_run.py
"""

import fcntl
import hashlib
import json
import logging
import os
import shutil
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

# ── Paths ─────────────────────────────────────────────────────────────
APP_DIR        = Path("/mnt/data/ares-app")
PHOTO_INDEX    = APP_DIR / "photo_index.json"
AI_DIR         = APP_DIR / "ai_data"
CLIP_EMB_FILE  = AI_DIR / "clip_embeddings.npy"
CLIP_HASHES    = AI_DIR / "clip_hashes.json"
FACE_CLUSTERS  = AI_DIR / "face_clusters.json"
SHA_CACHE      = AI_DIR / "sha256_cache.json"
TRASH_BASE     = Path("/mnt/data/.ares-trash")
LOCK_FILE      = Path("/mnt/data/ares-app/photo_index.json.lock")
PHOTOS_ROOT    = Path("/mnt/data/PHOTOS")

# Conservative CLIP threshold — 0.995 catches HEIC+JPG of same shot
CLIP_THRESHOLD = 0.995
# Format-rank: lower = worse (prefer highest rank)
FORMAT_RANK = {".heic": 10, ".dng": 9, ".raw": 9, ".arw": 9, ".cr2": 9,
               ".nef": 9, ".tif": 8, ".tiff": 8, ".png": 5,
               ".jpg": 4, ".jpeg": 4, ".mp4": 3, ".mov": 3}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("dedup")


# ── Atomic index writers ───────────────────────────────────────────────

def _atomic_write(path: Path, data):
    tmp = str(path) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, str(path))


def _save_photo_index(items):
    # Refuse to wipe the library: a dedup that drops >50% in one save is a bug, not
    # a dedup (one clobbered the index 46,906 -> 5 on Jun 28 2026). Mirrors the guard
    # in app.py:_save_photo_index so a standalone run can't bypass it.
    assert isinstance(items, list), "photo index must be a list"
    if PHOTO_INDEX.exists():
        try:
            with open(PHOTO_INDEX) as _cur:
                _prev = len(json.load(_cur))
        except Exception:
            _prev = 0
        assert not (_prev >= 100 and len(items) < _prev * 0.5), \
            f"refusing to shrink photo_index {_prev} -> {len(items)} (>50% drop)"
    tmp = str(PHOTO_INDEX) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(items, f)
    if PHOTO_INDEX.exists():
        try:
            with open(PHOTO_INDEX) as chk:
                json.load(chk)
            shutil.copy2(str(PHOTO_INDEX), str(PHOTO_INDEX) + ".bak")
        except Exception:
            pass
    os.replace(tmp, str(PHOTO_INDEX))


# ── SHA-256 cache ──────────────────────────────────────────────────────

def load_sha_cache() -> dict:
    if SHA_CACHE.exists():
        try:
            with open(SHA_CACHE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_sha_cache(cache: dict):
    _atomic_write(SHA_CACHE, cache)


def file_sha256(path: str, cache: dict) -> str | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = f"{path}:{st.st_mtime}:{st.st_size}"
    if key in cache:
        return cache[key]
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        digest = h.hexdigest()
        cache[key] = digest
        return digest
    except OSError:
        return None


# ── Keeper selection tiebreaker ────────────────────────────────────────

def keeper_score(path: str, date: float, size: int) -> tuple:
    """Higher score = prefer this as keeper."""
    ext = Path(path).suffix.lower()
    fmt = FORMAT_RANK.get(ext, 3)
    # (format_rank, size, -date [earlier is better], -pathlen)
    return (fmt, size, -date, -len(path))


# ── Trash helpers ──────────────────────────────────────────────────────

def trash_path(original: str, run_tag: str, kind: str) -> Path:
    """Compute destination path under trash, preserving structure."""
    rel = original.lstrip("/")
    return TRASH_BASE / f"{run_tag}-dedup-{kind}" / rel


def move_to_trash(src: str, dst: Path) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(src, str(dst))
        return True
    except Exception as e:
        log.warning(f"  move failed {src} -> {dst}: {e}")
        return False


def remove_thumb(thumb_url: str):
    """Delete thumb file if it exists (thumb and thumb_hq share hash but diff dirs)."""
    if not thumb_url:
        return
    # /static/thumbs/HASH.jpg  or  /static/thumbs_hq/HASH.jpg
    rel = thumb_url.lstrip("/")
    p = APP_DIR / rel
    if p.exists():
        try:
            p.unlink()
        except Exception:
            pass


# ── Face cluster cleanup ───────────────────────────────────────────────

def remove_from_face_clusters(thumb_hashes: set):
    """Remove trashed thumb hashes from face_clusters.json (atomic)."""
    if not FACE_CLUSTERS.exists() or not thumb_hashes:
        return 0
    try:
        with open(FACE_CLUSTERS) as f:
            fc = json.load(f)
    except Exception as e:
        log.warning(f"face_clusters read error: {e}")
        return 0

    removed = 0
    for cid, cluster in fc.items():
        ph = cluster.get("photo_hashes", [])
        before = len(ph)
        cluster["photo_hashes"] = [h for h in ph if h not in thumb_hashes]
        removed += before - len(cluster["photo_hashes"])
        cluster["photo_count"] = len(cluster["photo_hashes"])

    _atomic_write(FACE_CLUSTERS, fc)
    return removed


# ── Manifest writer ────────────────────────────────────────────────────

def write_manifest_line(mf, trashed_path: str, keeper_path: str,
                         reason: str, sim: float, group_id: str):
    line = json.dumps({
        "trashed_path": trashed_path,
        "keeper_path": keeper_path,
        "reason": reason,
        "sim": sim,
        "group_id": group_id,
    })
    mf.write(line + "\n")


# ══════════════════════════════════════════════════════════════════════
# PASS 1: Exact SHA-256 dedup
# ══════════════════════════════════════════════════════════════════════

def pass1_exact(idx: list, run_tag: str, manifest_path: Path) -> tuple[list, set]:
    """Returns (updated_idx, trashed_thumb_hashes)."""
    log.info("=== Pass 1: SHA-256 exact dedup ===")

    sha_cache = load_sha_cache()
    path_counts = defaultdict(int)
    for item in idx:
        path_counts[item["path"]] += 1

    # Hash every file
    sha_to_items = defaultdict(list)
    missing_paths = []
    dup_path_bugs = []
    save_interval = 500
    i = 0

    for item in idx:
        p = item["path"]
        if path_counts[p] > 1:
            dup_path_bugs.append(p)
            continue
        digest = file_sha256(p, sha_cache)
        if digest is None:
            missing_paths.append(item)
            continue
        sha_to_items[digest].append(item)
        i += 1
        if i % save_interval == 0:
            save_sha_cache(sha_cache)
            log.info(f"  hashed {i} files...")

    save_sha_cache(sha_cache)

    if dup_path_bugs:
        log.warning(f"  index bug: {len(set(dup_path_bugs))} paths appear >1 time — skipping, not trashing")

    log.info(f"  hashed {i} files, {len(missing_paths)} missing on disk, {len(sha_to_items)} unique SHAs")

    # Find dup groups
    dup_groups = {sha: items for sha, items in sha_to_items.items() if len(items) > 1}
    log.info(f"  exact dup groups: {len(dup_groups)}")

    trashed_paths = set()
    trashed_thumb_hashes = set()
    bytes_reclaimed = 0
    files_trashed = 0

    with open(manifest_path, "a") as mf:
        # Log missing as manifest entries
        for item in missing_paths:
            write_manifest_line(mf, item["path"], "", "missing", 0.0, "missing")

        for gid, (sha, members) in enumerate(dup_groups.items()):
            # Sort: highest score first = keeper
            members.sort(
                key=lambda m: keeper_score(
                    m["path"], m.get("date", 0), _safe_size(m["path"])
                ),
                reverse=True,
            )
            keeper = members[0]
            losers = members[1:]

            for loser in losers:
                src = loser["path"]
                dst = trash_path(src, run_tag, "exact")
                sz = _safe_size(src)
                if move_to_trash(src, dst):
                    trashed_paths.add(src)
                    # collect thumb hash for face cluster cleanup
                    th = _thumb_hash(loser)
                    if th:
                        trashed_thumb_hashes.add(th)
                    # remove thumb files
                    remove_thumb(loser.get("thumb", ""))
                    remove_thumb(loser.get("thumb_hq", ""))
                    bytes_reclaimed += sz
                    files_trashed += 1
                    write_manifest_line(
                        mf, src, keeper["path"], "exact", 1.0, f"exact-{gid}"
                    )

    # Remove trashed entries + missing from index
    missing_set = {item["path"] for item in missing_paths}
    new_idx = [
        item for item in idx
        if item["path"] not in trashed_paths and item["path"] not in missing_set
    ]
    log.info(f"  trashed {files_trashed} files, {bytes_reclaimed / 1024**3:.2f} GB reclaimed")
    log.info(f"  dropped {len(missing_paths)} missing entries from index")
    return new_idx, trashed_thumb_hashes


def _safe_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _thumb_hash(item: dict) -> str | None:
    url = item.get("thumb", "")
    if url:
        return url.rsplit("/", 1)[-1].replace(".jpg", "").replace(".webp", "")
    return None


# ══════════════════════════════════════════════════════════════════════
# PASS 2: CLIP near-duplicate dedup
# ══════════════════════════════════════════════════════════════════════

def pass2_clip(idx: list, run_tag: str, manifest_path: Path) -> tuple[list, set]:
    """Returns (updated_idx, trashed_thumb_hashes)."""
    log.info("=== Pass 2: CLIP near-dup (threshold=0.995) ===")

    if not CLIP_EMB_FILE.exists() or not CLIP_HASHES.exists():
        log.warning("  clip_embeddings.npy or clip_hashes.json missing — skipping Pass 2")
        return idx, set()

    with open(CLIP_HASHES) as f:
        clip_hashes = json.load(f)   # list of thumb_hashes, index-aligned with embeddings

    emb = np.load(str(CLIP_EMB_FILE), mmap_mode="r").astype(np.float32)
    N = emb.shape[0]
    log.info(f"  loaded {N} embeddings ({emb.shape[1]}-dim)")

    # Build thumb_hash -> (embedding_index, photo_index_item) map
    # Only include IMAGES (skip videos — no CLIP embeddings for them)
    th_to_idx_item = {}
    for item in idx:
        if item.get("type") == "video":
            continue
        th = _thumb_hash(item)
        if th:
            th_to_idx_item[th] = item

    # Filter embeddings to only those in current index
    valid_indices = []
    valid_hashes = []
    for i, h in enumerate(clip_hashes):
        if h in th_to_idx_item:
            valid_indices.append(i)
            valid_hashes.append(h)

    log.info(f"  {len(valid_indices)}/{N} embeddings correspond to current index entries")

    if len(valid_indices) < 2:
        log.info("  not enough valid embeddings for CLIP dedup")
        return idx, set()

    # Normalize embeddings (cosine sim = dot product of L2-normalized)
    E = emb[np.array(valid_indices)]  # shape (M, 512)
    norms = np.linalg.norm(E, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1, norms)
    E = E / norms

    # Chunked similarity search — avoid N^2 memory
    M = len(valid_indices)
    CHUNK = 1024
    # Union-Find for connected components
    parent = list(range(M))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    log.info(f"  running chunked cosine similarity ({M} x {M}) in chunks of {CHUNK}...")
    t0 = time.time()
    pairs_found = 0

    for start in range(0, M, CHUNK):
        chunk = E[start:start + CHUNK]  # (c, 512)
        # dot product with all vectors (cosine sim since normalized)
        sims = chunk @ E.T  # (c, M)
        rows, cols = np.where(sims >= CLIP_THRESHOLD)
        for r, c in zip(rows, cols):
            gi = start + r
            gj = int(c)
            if gi < gj:  # avoid self and duplicate pairs
                union(gi, gj)
                pairs_found += 1

        if (start // CHUNK) % 10 == 0:
            elapsed = time.time() - t0
            pct = min(100, (start + CHUNK) / M * 100)
            log.info(f"    {pct:.0f}% done  ({elapsed:.0f}s)")

    log.info(f"  similarity scan done in {time.time()-t0:.0f}s, {pairs_found} pairs above threshold")

    # Group by component
    from collections import defaultdict
    components = defaultdict(list)
    for i in range(M):
        components[find(i)].append(i)

    dup_groups = {root: members for root, members in components.items() if len(members) > 1}
    log.info(f"  CLIP near-dup groups: {len(dup_groups)}")

    trashed_paths = set()
    trashed_thumb_hashes = set()
    bytes_reclaimed = 0
    files_trashed = 0

    with open(manifest_path, "a") as mf:
        for gid, (root, members) in enumerate(dup_groups.items()):
            # Get items for each member index
            group_items = []
            for mi in members:
                h = valid_hashes[mi]
                item = th_to_idx_item.get(h)
                if item:
                    group_items.append((mi, item))

            if len(group_items) < 2:
                continue

            # Score each item — highest score = keeper
            group_items.sort(
                key=lambda t: keeper_score(
                    t[1]["path"], t[1].get("date", 0), _safe_size(t[1]["path"])
                ),
                reverse=True,
            )
            keeper_mi, keeper_item = group_items[0]
            losers = group_items[1:]

            # Get keeper's avg sim to losers (for manifest)
            for loser_mi, loser_item in losers:
                sim = float(E[keeper_mi] @ E[loser_mi])
                src = loser_item["path"]
                if src in trashed_paths:
                    continue  # already trashed by exact pass or earlier in this pass
                dst = trash_path(src, run_tag, "clip")
                sz = _safe_size(src)
                if move_to_trash(src, dst):
                    trashed_paths.add(src)
                    th = _thumb_hash(loser_item)
                    if th:
                        trashed_thumb_hashes.add(th)
                    remove_thumb(loser_item.get("thumb", ""))
                    remove_thumb(loser_item.get("thumb_hq", ""))
                    bytes_reclaimed += sz
                    files_trashed += 1
                    write_manifest_line(
                        mf, src, keeper_item["path"], "clip", sim, f"clip-{gid}"
                    )

    new_idx = [item for item in idx if item["path"] not in trashed_paths]
    log.info(f"  trashed {files_trashed} files, {bytes_reclaimed / 1024**3:.2f} GB reclaimed")
    return new_idx, trashed_thumb_hashes


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════

def main():
    now = datetime.now(timezone.utc)
    run_tag = now.strftime("%Y%m%d-%H%M%S")
    manifest_path = AI_DIR / f"dedup-manifest-{run_tag}.jsonl"

    log.info(f"ARES Dedup run {run_tag}")
    log.info(f"manifest -> {manifest_path}")

    # Acquire file lock
    lock_fd = open(str(LOCK_FILE), "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.error("photo_index.json.lock held by another process — aborting")
        sys.exit(1)

    try:
        # Snapshot index before any changes
        snapshot_name = PHOTO_INDEX.parent / f"photo_index.json.pre-dedup-{now.strftime('%Y%m%d')}"
        if not snapshot_name.exists():
            shutil.copy2(str(PHOTO_INDEX), str(snapshot_name))
            log.info(f"snapshot -> {snapshot_name}")
        else:
            log.info(f"snapshot already exists: {snapshot_name} (skipping)")

        # Load index
        with open(PHOTO_INDEX) as f:
            idx = json.load(f)
        log.info(f"loaded {len(idx)} index entries")

        # ── Pass 1 ──────────────────────────────────────────────────
        idx, p1_face_hashes = pass1_exact(idx, run_tag, manifest_path)
        _save_photo_index(idx)
        log.info(f"index saved after Pass 1 ({len(idx)} entries)")

        # Face cluster cleanup for Pass 1 losers
        if p1_face_hashes:
            removed = remove_from_face_clusters(p1_face_hashes)
            log.info(f"  removed {removed} face refs from clusters (Pass 1)")

        # ── Pass 2 ──────────────────────────────────────────────────
        idx, p2_face_hashes = pass2_clip(idx, run_tag, manifest_path)
        _save_photo_index(idx)
        log.info(f"index saved after Pass 2 ({len(idx)} entries)")

        if p2_face_hashes:
            removed = remove_from_face_clusters(p2_face_hashes)
            log.info(f"  removed {removed} face refs from clusters (Pass 2)")

        # ── Summary ─────────────────────────────────────────────────
        log.info("=== DONE ===")
        log.info(f"final index: {len(idx)} entries")
        log.info(f"manifest: {manifest_path}")

        # Count manifest lines by reason
        try:
            with open(manifest_path) as mf:
                lines = [json.loads(l) for l in mf if l.strip()]
            exact = [l for l in lines if l["reason"] == "exact"]
            clip  = [l for l in lines if l["reason"] == "clip"]
            miss  = [l for l in lines if l["reason"] == "missing"]
            log.info(f"  exact: {len(exact)} trashed, clip: {len(clip)} trashed, missing dropped: {len(miss)}")
        except Exception:
            pass

    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    main()
