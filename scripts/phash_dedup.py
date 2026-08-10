#!/usr/bin/env python3
"""pHash dedup pass for ARES. 8x8 DCT, no imagehash lib required."""
import json, os, sys, shutil, fcntl
from datetime import datetime
from collections import defaultdict
import numpy as np
from PIL import Image
from scipy.fftpack import dct as scipy_dct

APP_DIR = "/mnt/data/ares-app"
PHOTO_INDEX_PATH = os.path.join(APP_DIR, "photo_index.json")
LOCK_PATH = PHOTO_INDEX_PATH + ".lock"
AI_DIR = os.path.join(APP_DIR, "ai_data")
PHASH_CACHE_PATH = os.path.join(AI_DIR, "phash_cache.json")
VIDFRAMES_DIR = os.path.join(AI_DIR, "vidframes")
TRASH_BASE = "/mnt/data/.ares-trash"
RUN_TS = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
MANIFEST_PATH = os.path.join(AI_DIR, "dedup-manifest-phash-" + RUN_TS + ".jsonl")
SNAPSHOT_PATH = PHOTO_INDEX_PATH + ".pre-phash-" + datetime.utcnow().strftime("%Y%m%d")
TRASH_DIR = os.path.join(TRASH_BASE, RUN_TS + "-dedup-phash")
PRIOR_MANIFESTS = [
    os.path.join(AI_DIR, "dedup-manifest-20260419-230520.jsonl"),
    os.path.join(AI_DIR, "dedup-manifest-v2-20260419-234555.jsonl"),
]
HAMMING_THRESHOLD = 2


def compute_phash(img_path):
    try:
        img = Image.open(img_path).convert("L").resize((32, 32), Image.LANCZOS)
        pixels = np.array(img, dtype=float)
        dct_rows = scipy_dct(pixels, axis=1, norm="ortho")
        dct_2d = scipy_dct(dct_rows, axis=0, norm="ortho")
        top = dct_2d[:8, :8].flatten()
        median = np.median(top)
        bits = top > median
        h = 0
        for b in bits:
            h = (h << 1) | int(b)
        return h
    except Exception:
        return None


def hamming_dist(a, b):
    x = a ^ b
    count = 0
    while x:
        count += x & 1
        x >>= 1
    return count


def load_prior_manifest_paths():
    covered = set()
    for mf in PRIOR_MANIFESTS:
        if not os.path.exists(mf):
            continue
        try:
            with open(mf) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    for k in ("trashed_path", "keeper_path"):
                        p = rec.get(k, "")
                        if p:
                            covered.add(p)
        except Exception:
            pass
    return covered


def load_phash_cache():
    if os.path.exists(PHASH_CACHE_PATH):
        try:
            with open(PHASH_CACHE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_phash_cache(cache):
    tmp = PHASH_CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, PHASH_CACHE_PATH)


def save_photo_index(items):
    # Refuse to wipe the library: a dedup that drops >50% in one save is a bug, not
    # a dedup (one clobbered the index 46,906 -> 5 on Jun 28 2026). Mirrors the guard
    # in app.py:_save_photo_index so a standalone run can't bypass it.
    assert isinstance(items, list), "photo index must be a list"
    if os.path.exists(PHOTO_INDEX_PATH):
        try:
            with open(PHOTO_INDEX_PATH) as _cur:
                _prev = len(json.load(_cur))
        except Exception:
            _prev = 0
        assert not (_prev >= 100 and len(items) < _prev * 0.5), \
            f"refusing to shrink photo_index {_prev} -> {len(items)} (>50% drop)"
    tmp = PHOTO_INDEX_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(items, f)
    if os.path.exists(PHOTO_INDEX_PATH):
        try:
            with open(PHOTO_INDEX_PATH) as chk:
                json.load(chk)
            shutil.copy2(PHOTO_INDEX_PATH, PHOTO_INDEX_PATH + ".bak")
        except Exception:
            pass
    os.replace(tmp, PHOTO_INDEX_PATH)


def tiebreak_keep(group_items):
    if len(group_items) == 1:
        return group_items[0], []
    images = [it for it in group_items if it.get("type") != "video"]
    videos = [it for it in group_items if it.get("type") == "video"]
    if images and videos:
        max_vid_size = max(v.get("_fsize", 0) for v in videos)
        max_img_size = max(i.get("_fsize", 0) for i in images)
        if max_vid_size > 0 and max_vid_size > 5 * max_img_size:
            candidates = videos
        else:
            candidates = images
    else:
        candidates = list(group_items)
    heics = [c for c in candidates if c["path"].upper().endswith(".HEIC")]
    others = [c for c in candidates if not c["path"].upper().endswith(".HEIC")]
    if heics and others:
        max_heic_sz = max(h.get("_fsize", 0) for h in heics)
        if all(h.get("_fsize", 0) > 0 for h in heics) and max_heic_sz > 0:
            candidates = heics
    candidates.sort(key=lambda x: (-x.get("_fsize", 0), x.get("date", 0), len(x["path"])))
    keeper = candidates[0]
    losers = [it for it in group_items if it["path"] != keeper["path"]]
    return keeper, losers


def union_find_group(pairs):
    parent = {}
    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    all_nodes = set()
    for a, b in pairs:
        union(a, b)
        all_nodes.add(a)
        all_nodes.add(b)
    groups = defaultdict(list)
    for node in all_nodes:
        groups[find(node)].append(node)
    return list(groups.values())


def main():
    print("[phash] Run " + RUN_TS)
    lock_fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("[phash] photo_index.json.lock held -- aborting")
        sys.exit(1)
    try:
        with open(PHOTO_INDEX_PATH) as f:
            items = json.load(f)
        print("[phash] Loaded " + str(len(items)) + " items")
        if not os.path.exists(SNAPSHOT_PATH):
            shutil.copy2(PHOTO_INDEX_PATH, SNAPSHOT_PATH)
            print("[phash] Snapshot: " + SNAPSHOT_PATH)
        covered = load_prior_manifest_paths()
        print("[phash] Prior manifest covers " + str(len(covered)) + " paths")
        cache = load_phash_cache()
        print("[phash] pHash cache: " + str(len(cache)) + " entries")
        candidates = []
        for item in items:
            p = item["path"]
            if p in covered:
                continue
            if item.get("type") == "video":
                thumb = item.get("thumb", "")
                if thumb:
                    h = thumb.rsplit("/", 1)[-1].replace(".jpg", "")
                    vf = os.path.join(VIDFRAMES_DIR, h + ".jpg")
                    if os.path.exists(vf):
                        candidates.append((vf, item, "vf:" + p))
            else:
                if os.path.exists(p):
                    candidates.append((p, item, "img:" + p))
        print("[phash] Hashing " + str(len(candidates)) + " candidates...")
        path_to_hash = {}
        item_for_path = {}
        new_cache = 0
        for i, (abs_path, item, cache_key) in enumerate(candidates):
            if i % 1000 == 0:
                print("[phash]   " + str(i) + "/" + str(len(candidates)) + "...")
                sys.stdout.flush()
            try:
                st = os.stat(abs_path)
                ck = cache_key + ":" + str(round(st.st_mtime, 3)) + ":" + str(st.st_size)
            except OSError:
                continue
            if ck in cache:
                ph = cache[ck]
            else:
                ph = compute_phash(abs_path)
                if ph is None:
                    continue
                cache[ck] = ph
                new_cache += 1
            item_path = item["path"]
            try:
                item["_fsize"] = os.path.getsize(item_path) if os.path.exists(item_path) else 0
            except OSError:
                item["_fsize"] = 0
            path_to_hash[item_path] = ph
            item_for_path[item_path] = item
        print("[phash] " + str(len(path_to_hash)) + " hashes (" + str(new_cache) + " new). Saving cache...")
        save_phash_cache(cache)
        all_paths = list(path_to_hash.keys())
        all_hashes = [path_to_hash[p] for p in all_paths]
        n = len(all_paths)
        print("[phash] Finding pairs (Hamming <= " + str(HAMMING_THRESHOLD) + ") among " + str(n) + " items...")
        hash_to_paths = defaultdict(list)
        for p, h in path_to_hash.items():
            hash_to_paths[h].append(p)
        seen_pairs = set()
        pairs = []
        for h, ps in hash_to_paths.items():
            if len(ps) > 1:
                for i2 in range(len(ps)):
                    for j2 in range(i2 + 1, len(ps)):
                        key = (min(ps[i2], ps[j2]), max(ps[i2], ps[j2]))
                        if key not in seen_pairs:
                            seen_pairs.add(key)
                            pairs.append((ps[i2], ps[j2]))
        idx_sorted = sorted(range(n), key=lambda i2: all_hashes[i2])
        sorted_hashes = [all_hashes[idx_sorted[i2]] for i2 in range(n)]
        sorted_paths = [all_paths[idx_sorted[i2]] for i2 in range(n)]
        WINDOW = 3000
        for i2 in range(n):
            hi = sorted_hashes[i2]
            for j2 in range(i2 + 1, min(i2 + WINDOW, n)):
                hj = sorted_hashes[j2]
                xor = hi ^ hj
                if xor == 0:
                    continue
                d = bin(xor).count("1")
                if d <= HAMMING_THRESHOLD:
                    pi, pj = sorted_paths[i2], sorted_paths[j2]
                    key = (min(pi, pj), max(pi, pj))
                    if key not in seen_pairs:
                        seen_pairs.add(key)
                        pairs.append((pi, pj))
        print("[phash] " + str(len(pairs)) + " pairs. Building components...")
        components = union_find_group(pairs)
        components = [c for c in components if len(c) > 1]
        print("[phash] " + str(len(components)) + " duplicate groups")
        if not components:
            print("[phash] No duplicates. Done.")
            return
        os.makedirs(TRASH_DIR, exist_ok=True)
        manifest_lines = []
        paths_to_remove = set()
        total_bytes = 0
        groups_ok = 0
        for gid, group_paths in enumerate(components):
            group_items = [item_for_path[p] for p in group_paths if p in item_for_path]
            if len(group_items) < 2:
                continue
            keeper, losers = tiebreak_keep(group_items)
            keeper_path = keeper["path"]
            for loser in losers:
                loser_path = loser["path"]
                if not os.path.exists(loser_path):
                    continue
                rel = os.path.relpath(loser_path, "/")
                trash_dest = os.path.join(TRASH_DIR, rel)
                os.makedirs(os.path.dirname(trash_dest), exist_ok=True)
                try:
                    shutil.move(loser_path, trash_dest)
                    fsize = loser.get("_fsize", 0)
                    total_bytes += fsize
                    paths_to_remove.add(loser_path)
                    hk = path_to_hash.get(keeper_path, 0)
                    hl = path_to_hash.get(loser_path, 0)
                    hd = hamming_dist(hk, hl)
                    manifest_lines.append(json.dumps({
                        "trashed_path": loser_path,
                        "keeper_path": keeper_path,
                        "reason": "phash",
                        "hamming": hd,
                        "group_id": "phash-" + str(gid)
                    }))
                except Exception as e:
                    print("[phash] WARN: failed to trash " + loser_path + ": " + str(e))
            groups_ok += 1
        print("[phash] Trashing " + str(len(paths_to_remove)) + " files (" + str(round(total_bytes/1e9, 2)) + " GB)")
        with open(MANIFEST_PATH, "w") as f:
            for line in manifest_lines:
                f.write(line + chr(10))
        print("[phash] Manifest: " + MANIFEST_PATH)
        items_clean = [it for it in items if it["path"] not in paths_to_remove]
        for it in items_clean:
            it.pop("_fsize", None)
        save_photo_index(items_clean)
        print("[phash] Index: " + str(len(items)) + " -> " + str(len(items_clean)))
        print("")
        print("[phash] DONE")
        print("  Groups:    " + str(groups_ok))
        print("  Trashed:   " + str(len(paths_to_remove)))
        print("  Reclaimed: " + str(round(total_bytes/1e9, 2)) + " GB")
        print("  Manifest:  " + MANIFEST_PATH)
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


if __name__ == "__main__":
    main()
