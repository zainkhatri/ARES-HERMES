#!/usr/bin/env python3
"""
Rebuild duplicate_hashes.json from scratch.
- Backs up existing file
- Preserves 1,242 still-valid entries
- Detects new dupes by SHA256 (exact) and pHash (near) with per-source-combo thresholds
- Writes atomic update + jsonl manifest
- Does NOT touch photo_index.json or restart anything
"""
import json
import os
import sys
import hashlib
import time
from collections import defaultdict
from PIL import Image
import numpy as np

APP_DIR = "/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD"
PHOTO_INDEX_PATH = os.path.join(APP_DIR, "photo_index.json")
AI_DIR = os.path.join(APP_DIR, "ai_data")
DUP_HASHES_PATH = os.path.join(AI_DIR, "duplicate_hashes.json")
PHASH_CACHE_PATH = os.path.join(AI_DIR, "phash_cache.json")
PHOTOS_ROOT_HOST = "/mnt/nvme/PROMETHEUS/PHOTOS"

RUN_TS = int(time.time())
MANIFEST_PATH = os.path.join(AI_DIR, f"dedup-manifest-rebuild-{RUN_TS}.jsonl")
BAK_PATH = DUP_HASHES_PATH + f".bak-rebuild-{RUN_TS}"

# Per-combo phash thresholds.
# GOOGLE-internal and SNAPCHAT-internal have high burst rates — require strict threshold + same dims.
# Cross-camera combos (user syncs camera+phone) are reliably identical at dist=0-2.
# GOOGLE+SNAPCHAT and SNAPCHAT-internal: skip (confirmed distinct shots at dist 26-40).
SKIP_COMBOS = frozenset([
    frozenset(["GOOGLE", "SNAPCHAT"]),
    frozenset(["SNAPCHAT"]),
])

# Same-source thresholds: only collapse if phash AND same dimensions
# For same-source groups, pHash is unreliable (burst-shot collisions).
# Use SHA256 exact-match only for same-source — handled in the SHA256 pass above.

# Cross-source thresholds by combo: dist <= threshold
CROSS_SOURCE_THRESHOLDS = {
    # Camera+phone combos: all confirmed true-dupes are phash_dist=0.
    # dist=2 and dist=6 produced false positives (70% and 20% pixel diff).
    # Strict dist=0 only.
    frozenset(["S95", "iPhone"]): 0,
    frozenset(["RX100", "iPhone"]): 0,
    frozenset(["GX9", "iPhone"]): 0,
    frozenset(["G7X", "iPhone"]): 0,
    frozenset(["FUJI", "iPhone"]): 0,
    frozenset(["S95", "RX100"]): 0,
    frozenset(["S95", "GX9"]): 0,
    frozenset(["GOOGLE", "iPhone"]): 0,
    frozenset(["GOOGLE", "S95"]): 0,
    frozenset(["GOOGLE", "RX100"]): 0,
    frozenset(["PHOTOS", "iPhone"]): 0,  # same-file different import path
}
DEFAULT_CROSS_THRESHOLD = 0   # conservative default: exact phash match only

# Camera-origin sources (highest keep priority)
CAMERA_SOURCES = {"S95", "RX100", "GX9", "G7X", "FUJI", "PIXPRO"}

# Source priority for keeper selection (lower index = higher priority)
SOURCE_PRIORITY = ["S95", "RX100", "GX9", "G7X", "FUJI", "PIXPRO", "SNAPCHAT", "ipod-5th-gen", "iPhone", "GOOGLE", "PHOTOS"]


def resolve_path(index_path):
    assert isinstance(index_path, str) and index_path, "index_path must be non-empty str"
    if os.path.isfile(index_path):
        return index_path
    for marker in ("/PHOTOS/PHOTOS/", "/PHOTOS/"):
        if marker in index_path:
            rel = index_path.split(marker, 1)[1]
            cand = os.path.join(PHOTOS_ROOT_HOST, rel)
            if os.path.isfile(cand):
                return cand
    return None


def get_thumbkey(item):
    assert isinstance(item, dict), "item must be dict"
    thumb = item.get("thumb", "")
    if not thumb:
        return None
    return thumb.rsplit("/", 1)[-1].replace(".jpg", "")


def get_source(path):
    assert isinstance(path, str) and path, "path must be non-empty str"
    for marker in ("/PHOTOS/PHOTOS/", "/PHOTOS/"):
        if marker in path:
            rel = path.split(marker, 1)[1]
            return rel.split("/")[0]
    return "UNKNOWN"


def dct_1d(x):
    """DCT-II via FFT, ortho normalization. Pure numpy — no scipy needed."""
    N = len(x)
    assert N > 0, "dct_1d requires non-empty input"
    v = np.zeros(N)
    v[: N // 2] = x[::2]
    v[N // 2 :] = x[1::2][::-1]
    V = np.fft.fft(v)
    k = np.arange(N)
    w = 2 * np.exp(-1j * np.pi * k / (2 * N))
    w[0] = w[0] * (1.0 / np.sqrt(4 * N))
    w[1:] = w[1:] * (1.0 / np.sqrt(2 * N))
    return (V * w).real


def compute_phash(img_path):
    assert isinstance(img_path, str) and img_path, "img_path must be non-empty str"
    try:
        img = Image.open(img_path).convert("L").resize((32, 32), Image.LANCZOS)
        pixels = np.array(img, dtype=float)
        dct_rows = np.apply_along_axis(dct_1d, 1, pixels)
        dct_2d = np.apply_along_axis(dct_1d, 0, dct_rows)
        top = dct_2d[:8, :8].flatten()
        median = np.median(top)
        bits = (top > median).astype(int)
        h = 0
        for b in bits:
            h = (h << 1) | int(b)
        return h
    except Exception:
        return None


def hamming_dist(a, b):
    assert isinstance(a, int) and isinstance(b, int), "hamming_dist requires int inputs"
    return bin(a ^ b).count("1")


def compute_sha256(path):
    assert isinstance(path, str) and path, "path must be non-empty str"
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def get_dims(path):
    assert isinstance(path, str) and path, "path must be non-empty str"
    try:
        with Image.open(path) as img:
            return img.size
    except Exception:
        return (0, 0)


def source_priority_key(src):
    try:
        return SOURCE_PRIORITY.index(src)
    except ValueError:
        return len(SOURCE_PRIORITY)


def pick_keeper(group_items):
    """Return (keeper, [losers]) following the task's keep-priority rules."""
    assert len(group_items) >= 2, "pick_keeper requires at least 2 items"

    def sort_key(it):
        src = get_source(it["path"])
        fsize = it.get("_fsize", 0)
        return (source_priority_key(src), -fsize)

    sorted_items = sorted(group_items, key=sort_key)
    keeper = sorted_items[0]
    losers = sorted_items[1:]
    return keeper, losers


def load_phash_cache():
    if os.path.exists(PHASH_CACHE_PATH):
        try:
            with open(PHASH_CACHE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_phash_cache(cache):
    assert isinstance(cache, dict), "cache must be dict"
    tmp = PHASH_CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, PHASH_CACHE_PATH)


def union_find_group(pairs):
    """Union-find to build connected components from (a, b) pairs."""
    assert isinstance(pairs, list), "pairs must be list"
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
    print(f"[rebuild-dedup] Run {RUN_TS}")

    # Step 1: Load index
    with open(PHOTO_INDEX_PATH) as f:
        items = json.load(f)
    print(f"[rebuild-dedup] Index: {len(items)} items")

    # Step 2: Build current thumb-key set
    all_thumbkeys = {}  # thumbkey -> item
    for item in items:
        k = get_thumbkey(item)
        if k:
            all_thumbkeys[k] = item

    # Step 3: Load existing dup_hashes and keep valid subset
    existing_dup_hashes = json.load(open(DUP_HASHES_PATH))
    existing_set = set(existing_dup_hashes)
    valid_existing = [h for h in existing_dup_hashes if h in all_thumbkeys]
    print(f"[rebuild-dedup] Existing: {len(existing_set)} entries, {len(valid_existing)} still valid")

    # Backup
    import shutil
    shutil.copy2(DUP_HASHES_PATH, BAK_PATH)
    print(f"[rebuild-dedup] Backup: {BAK_PATH}")

    # Step 4: Identify visible (non-hidden) image items
    dup_hashes_seed = set(valid_existing)
    visible_images = []
    for item in items:
        if item.get("type") == "video":
            continue
        key = get_thumbkey(item)
        if key and key in dup_hashes_seed:
            continue
        visible_images.append(item)
    print(f"[rebuild-dedup] Visible images (excl already-hidden): {len(visible_images)}")
    visible_before = len(visible_images)

    # Step 5: Group by 3-second capture-time bucket
    bucket_map = defaultdict(list)
    for item in visible_images:
        ts = item.get("date", 0)
        if not ts:
            continue
        bucket = int(ts) // 3
        bucket_map[bucket].append(item)

    multi_groups = {k: v for k, v in bucket_map.items() if len(v) > 1}
    print(f"[rebuild-dedup] Multi-item 3s-bucket groups: {len(multi_groups)}")

    # Step 6: Resolve paths and compute file sizes
    item_fsize = {}
    item_realpath = {}
    for item in visible_images:
        p = item["path"]
        rp = resolve_path(p)
        if rp:
            item_realpath[p] = rp
            try:
                item_fsize[p] = os.path.getsize(rp)
            except OSError:
                item_fsize[p] = 0
        else:
            item_fsize[p] = 0

    # Step 7: SHA256 pass — exact duplicates get hidden regardless of source
    print("[rebuild-dedup] SHA256 pass...")
    sha_map = defaultdict(list)  # sha256 -> [items with that hash]
    sha_processed = 0
    for item in visible_images:
        p = item["path"]
        rp = item_realpath.get(p)
        if not rp:
            continue
        sha = compute_sha256(rp)
        if sha:
            sha_map[sha].append(item)
        sha_processed += 1
        if sha_processed % 5000 == 0:
            print(f"[rebuild-dedup]   sha256: {sha_processed}/{len(visible_images)}")

    sha_pairs = []
    for sha, grp in sha_map.items():
        if len(grp) < 2:
            continue
        for i in range(len(grp)):
            for j in range(i + 1, len(grp)):
                sha_pairs.append((grp[i]["path"], grp[j]["path"]))

    print(f"[rebuild-dedup] SHA256 exact pairs: {len(sha_pairs)}")

    # Step 8: pHash pass — near-duplicates within 3s groups
    print("[rebuild-dedup] pHash pass...")
    phash_cache = load_phash_cache()
    phash_map = {}  # index_path -> phash int
    new_cache = 0

    for item in visible_images:
        p = item["path"]
        rp = item_realpath.get(p)
        if not rp:
            continue
        try:
            st = os.stat(rp)
            ck = rp + ":" + str(round(st.st_mtime, 3)) + ":" + str(st.st_size)
        except OSError:
            continue
        if ck in phash_cache:
            ph = phash_cache[ck]
        else:
            ph = compute_phash(rp)
            if ph is None:
                continue
            phash_cache[ck] = ph
            new_cache += 1
        phash_map[p] = ph

    print(f"[rebuild-dedup] pHash computed: {len(phash_map)} ({new_cache} new). Saving cache...")
    save_phash_cache(phash_cache)

    # Step 9: Find pHash pairs within 3s groups, apply per-combo thresholds
    phash_pairs = []
    skipped_combos = defaultdict(int)
    processed_combos = defaultdict(int)

    for bucket, grp in multi_groups.items():
        # Build list of (item, source) with phash available
        candidates = []
        for item in grp:
            p = item["path"]
            if p not in phash_map:
                continue
            src = get_source(p)
            candidates.append((item, src))

        if len(candidates) < 2:
            continue

        sources_in_group = set(src for _, src in candidates)
        combo_key = frozenset(sources_in_group)

        # Skip combos known to be distinct shots
        if combo_key in SKIP_COMBOS:
            skipped_combos[str(sorted(sources_in_group))] += 1
            continue

        # Determine threshold and whether same-dims required
        if len(sources_in_group) == 1:
            # Same-source: pHash is too coarse (collisions on burst shots).
            # Skip — SHA256 exact-match handles true same-source dupes.
            continue
        else:
            threshold = CROSS_SOURCE_THRESHOLDS.get(combo_key, DEFAULT_CROSS_THRESHOLD)

        # Check all pairs in the group
        for i in range(len(candidates)):
            for j in range(i + 1, len(candidates)):
                it_i, src_i = candidates[i]
                it_j, src_j = candidates[j]
                pi = it_i["path"]
                pj = it_j["path"]
                phi = phash_map[pi]
                phj = phash_map[pj]
                d = hamming_dist(phi, phj)
                if d > threshold:
                    continue
                phash_pairs.append((pi, pj, d))
                processed_combos[str(sorted(sources_in_group))] += 1

    print(f"[rebuild-dedup] pHash near-pairs found: {len(phash_pairs)}")
    print(f"[rebuild-dedup] Combos processed: {dict(processed_combos)}")
    print(f"[rebuild-dedup] Combos skipped: {dict(skipped_combos)}")

    # Step 10: Union-find to build dupe groups from all pairs
    all_pairs = [(a, b) for a, b in sha_pairs] + [(a, b) for a, b, _ in phash_pairs]
    components = union_find_group(all_pairs)
    components = [c for c in components if len(c) > 1]
    print(f"[rebuild-dedup] Dupe groups (after union-find): {len(components)}")

    # Build lookup: path -> item
    path_to_item = {item["path"]: item for item in visible_images}

    # Step 11: For each group, pick keeper and mark losers
    new_hidden_keys = []
    manifest_lines = []
    groups_with_no_keeper = 0
    groups_processed = 0

    for gid, group_paths in enumerate(components):
        group_items = [path_to_item[p] for p in group_paths if p in path_to_item]
        if len(group_items) < 2:
            continue

        # Attach file sizes for keeper selection
        for it in group_items:
            it["_fsize"] = item_fsize.get(it["path"], 0)

        keeper, losers = pick_keeper(group_items)

        assert keeper is not None, f"keeper must not be None (group {gid})"
        assert len(losers) >= 1, f"must have at least one loser (group {gid})"

        keeper_key = get_thumbkey(keeper)
        assert keeper_key is not None, f"keeper must have a thumb key (group {gid})"

        # Verify keeper is not already in the exclusion set
        if keeper_key in dup_hashes_seed:
            # Keeper was previously hidden — this is a logic error; skip group
            groups_with_no_keeper += 1
            continue

        dt_map = {}
        for it in group_items:
            for it2 in group_items:
                if it is not it2:
                    dt_map[(it["path"], it2["path"])] = abs(it.get("date", 0) - it2.get("date", 0))

        for loser in losers:
            loser_key = get_thumbkey(loser)
            if not loser_key:
                continue
            if loser_key in dup_hashes_seed:
                continue  # already hidden

            # Determine method
            loser_p = loser["path"]
            keeper_p = keeper["path"]
            ph_l = phash_map.get(loser_p)
            ph_k = phash_map.get(keeper_p)
            if ph_l is not None and ph_k is not None:
                hd = hamming_dist(ph_l, ph_k)
                method = f"phash{hd}"
            else:
                method = "sha"

            dt_seconds = dt_map.get((loser_p, keeper_p), 0)
            new_hidden_keys.append(loser_key)
            manifest_lines.append(json.dumps({
                "hidden_path": loser_p,
                "kept_path": keeper_p,
                "method": method,
                "dt_seconds": round(dt_seconds, 2),
                "hidden_src": get_source(loser_p),
                "kept_src": get_source(keeper_p),
                "group_id": f"rebuild-{gid}",
            }))

        groups_processed += 1

    print(f"[rebuild-dedup] Groups processed: {groups_processed}, skipped (keeper was hidden): {groups_with_no_keeper}")

    # Step 12: Assert every group still has >=1 visible member
    # Load screenshot hashes so groups where ALL members are screenshots don't
    # false-trip the safety check (those items are intentionally hidden elsewhere).
    ss_path = os.path.join(AI_DIR, "screenshot_hashes.json")
    screenshot_hashes = set()
    if os.path.exists(ss_path):
        try:
            with open(ss_path) as _sf:
                screenshot_hashes = set(json.load(_sf))
        except (OSError, ValueError):
            pass
    final_hidden = dup_hashes_seed | set(new_hidden_keys)
    violations = 0
    for gid, group_paths in enumerate(components):
        group_items = [path_to_item[p] for p in group_paths if p in path_to_item]
        if len(group_items) < 2:
            continue
        # A group is fully hidden only if every member is hidden by DEDUP (not just screenshots)
        visible_in_group = [it for it in group_items if get_thumbkey(it) not in final_hidden]
        if len(visible_in_group) == 0:
            # Check if every member is a screenshot — that's intentional, not a violation
            all_screenshots = all(get_thumbkey(it) in screenshot_hashes for it in group_items)
            if not all_screenshots:
                print(f"  VIOLATION group {gid}: all {len(group_items)} members hidden!")
                violations += 1

    assert violations == 0, f"SAFETY: {violations} groups have no visible member — aborting"
    print(f"[rebuild-dedup] Safety check passed: 0 groups fully hidden")

    # Step 13: Build final list and write atomically
    final_list = sorted(final_hidden)
    tmp_path = DUP_HASHES_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(final_list, f)
    os.replace(tmp_path, DUP_HASHES_PATH)
    print(f"[rebuild-dedup] Wrote {DUP_HASHES_PATH}: {len(final_list)} entries")

    # Step 14: Write manifest
    with open(MANIFEST_PATH, "w") as f:
        for line in manifest_lines:
            f.write(line + "\n")
    print(f"[rebuild-dedup] Manifest: {MANIFEST_PATH} ({len(manifest_lines)} lines)")

    # Step 15: Report final counts
    newly_hidden = len(new_hidden_keys)
    visible_after = visible_before - newly_hidden
    print("")
    print("=== SUMMARY ===")
    print(f"  Index items:           {len(items)}")
    print(f"  Visible before:        {visible_before}")
    print(f"  Previously valid hides:{len(valid_existing)}")
    print(f"  New items hidden:      {newly_hidden}")
    print(f"  Visible after:         {visible_after}")
    print(f"  duplicate_hashes.json: {len(final_list)} entries")
    print(f"  Dupe groups found:     {groups_processed}")
    print(f"  Manifest:              {MANIFEST_PATH}")

    # Step 16: Spot-check 8 hidden pairs across combos
    print("")
    print("=== SPOT-CHECK (8 hidden pairs) ===")
    spot_manifest = []
    seen_combos_spot = set()
    for line in manifest_lines:
        rec = json.loads(line)
        combo = (rec["hidden_src"], rec["kept_src"])
        if combo not in seen_combos_spot:
            seen_combos_spot.add(combo)
            spot_manifest.append(rec)
        if len(spot_manifest) >= 8:
            break

    for i, rec in enumerate(spot_manifest):
        hidden_rp = item_realpath.get(rec["hidden_path"])
        kept_rp = item_realpath.get(rec["kept_path"])
        hidden_sz = item_fsize.get(rec["hidden_path"], 0)
        kept_sz = item_fsize.get(rec["kept_path"], 0)
        print(f"  [{i+1}] method={rec['method']} dt={rec['dt_seconds']}s {rec['hidden_src']}->{rec['kept_src']}")
        print(f"       hidden: {rec['hidden_path'][-65:]}")
        print(f"       kept:   {rec['kept_path'][-65:]}")
        print(f"       sizes:  {hidden_sz//1024}K hidden / {kept_sz//1024}K kept")

    # Step 17: Recount residual same-3s groups
    print("")
    print("=== RESIDUAL DUPLICATE CHECK ===")
    residual_visible = [it for it in visible_images if get_thumbkey(it) not in final_hidden]
    # Also add back items from valid_existing that are still in index (they were never visible anyway)
    residual_bucket_map = defaultdict(list)
    for item in residual_visible:
        ts = item.get("date", 0)
        if not ts:
            continue
        bucket = int(ts) // 3
        residual_bucket_map[bucket].append(item)

    residual_multi = {k: v for k, v in residual_bucket_map.items() if len(v) > 1}
    residual_extra = sum(len(v) - 1 for v in residual_multi.values())

    residual_by_combo = defaultdict(int)
    for bucket, grp in residual_multi.items():
        srcs = sorted(set(get_source(it["path"]) for it in grp))
        residual_by_combo["+".join(srcs)] += 1

    print(f"  Residual 3s-bucket multi groups: {len(residual_multi)}")
    print(f"  Residual extra items: {residual_extra}")
    print("  By combo:")
    for combo, cnt in sorted(residual_by_combo.items(), key=lambda x: -x[1])[:15]:
        print(f"    {combo}: {cnt}")


if __name__ == "__main__":
    main()
