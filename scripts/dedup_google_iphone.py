#!/usr/bin/env python3
"""
Eliminate visible Google<->iPhone same-filename duplicates from the gallery.

Strategy: hide the GOOGLE copy when an iPhone copy exists with the same filename
and a EXIF-date difference of < 300 seconds. Writes losers' md5-of-rel-path hashes
into ai_data/duplicate_hashes.json using the same mechanism as _get_duplicate_hashes().

Does NOT touch photo_index.json, does NOT move/delete files.
"""

import hashlib
import json
import os
import sys
import time
from collections import defaultdict

APP_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(APP_DIR, "photo_index.json")
DUP_HASHES_PATH = os.path.join(APP_DIR, "ai_data", "duplicate_hashes.json")
MANIFEST_PATH = os.path.join(
    APP_DIR,
    "ai_data",
    f"dedup-manifest-google-iphone-{int(time.time())}.jsonl",
)

INDEX_PREFIX = "/mnt/data/PHOTOS/PHOTOS"
DATE_THRESHOLD_S = 300  # 5 minutes


def rel_path_from_index(index_path):
    assert index_path.startswith(INDEX_PREFIX + "/"), f"unexpected path: {index_path}"
    return index_path[len(INDEX_PREFIX) + 1:]


def path_hash(rel_path):
    assert rel_path and "/" not in rel_path[:1], "rel_path must not start with /"
    return hashlib.md5(rel_path.encode()).hexdigest()


def atomic_write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def load_index():
    with open(INDEX_PATH) as f:
        return json.load(f)


def load_dup_hashes():
    if not os.path.exists(DUP_HASHES_PATH):
        return set()
    with open(DUP_HASHES_PATH) as f:
        return set(json.load(f))


def main():
    items = load_index()
    existing_dup_hashes = load_dup_hashes()

    assert len(items) > 0, "index is empty"
    assert isinstance(existing_dup_hashes, set), "dup_hashes must be a set"

    # Build visible items (not already hidden)
    visible = []
    for item in items:
        raw = item.get("path", "")
        if not raw.startswith(INDEX_PREFIX + "/"):
            continue
        rel = rel_path_from_index(raw)
        h = hashlib.md5(rel.encode()).hexdigest()
        if h in existing_dup_hashes:
            continue
        src = rel.split("/")[0]
        fname = os.path.basename(raw).lower()
        visible.append(
            {
                "path": raw,
                "rel_path": rel,
                "src": src,
                "fname": fname,
                "date": item.get("date"),
                "hash": h,
            }
        )

    print(f"Visible items before: {len(visible)}")

    # Group by lowercase filename
    fname_map = defaultdict(list)
    for item in visible:
        fname_map[item["fname"]].append(item)

    # Identify hides: GOOGLE items with same filename + date < 300s of an iPhone item
    to_hide = []
    for fname, entries in fname_map.items():
        iphone_entries = [e for e in entries if e["src"] == "iPhone"]
        google_entries = [e for e in entries if e["src"] == "GOOGLE"]

        if not iphone_entries or not google_entries:
            continue

        for ge in google_entries:
            best_match = None
            best_diff = float("inf")
            for ie in iphone_entries:
                if ie["date"] and ge["date"]:
                    diff = abs(ie["date"] - ge["date"])
                    if diff < best_diff:
                        best_diff = diff
                        best_match = ie
            if best_match is not None and best_diff < DATE_THRESHOLD_S:
                to_hide.append(
                    {
                        "loser": ge,
                        "keeper": best_match,
                        "date_diff_s": best_diff,
                    }
                )

    print(f"Pairs to hide (GOOGLE, date_diff < {DATE_THRESHOLD_S}s): {len(to_hide)}")

    if not to_hide:
        print("Nothing to do.")
        return 0

    # Compute new hashes to add
    new_hashes = set()
    for rec in to_hide:
        loser_hash = rec["loser"]["hash"]
        assert loser_hash not in existing_dup_hashes, "already hidden: " + loser_hash
        new_hashes.add(loser_hash)

    # Guard: no keeper should be hidden
    keeper_hashes = {rec["keeper"]["hash"] for rec in to_hide}
    collision = new_hashes & keeper_hashes
    assert not collision, f"keeper/loser hash collision: {collision}"

    # Write manifest
    with open(MANIFEST_PATH, "w") as mf:
        for rec in to_hide:
            line = json.dumps(
                {
                    "loser_path": rec["loser"]["path"],
                    "keeper_path": rec["keeper"]["path"],
                    "reason": "same_fname_google_iphone",
                    "date_diff_s": rec["date_diff_s"],
                    "loser_src": rec["loser"]["src"],
                    "keeper_src": rec["keeper"]["src"],
                }
            )
            mf.write(line + "\n")
    print(f"Manifest: {MANIFEST_PATH}")

    # Update duplicate_hashes.json atomically
    merged = list(existing_dup_hashes | new_hashes)
    atomic_write_json(DUP_HASHES_PATH, merged)
    print(
        f"duplicate_hashes.json: {len(existing_dup_hashes)} -> {len(merged)} "
        f"(+{len(new_hashes)} new)"
    )

    # Verify: count visible after applying new dup set
    all_dup = existing_dup_hashes | new_hashes
    visible_after = sum(
        1
        for item in items
        if item.get("path", "").startswith(INDEX_PREFIX + "/")
        and hashlib.md5(
            rel_path_from_index(item["path"]).encode()
        ).hexdigest()
        not in all_dup
    )
    print(f"Visible items after: {visible_after}")
    print(f"Reduction: {len(visible) - visible_after}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
