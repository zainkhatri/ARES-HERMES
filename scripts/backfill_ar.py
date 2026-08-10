#!/usr/bin/env python3
"""Backfill `ar` (aspect ratio = width/height) onto every photo_index item so
the justified grid lays out at the CORRECT row heights. Without it itemAr()
returns 1 (square) for everything, the per-month height estimates are wrong,
and _reconcileMonthOffset shifts every offset as sections render — which makes
the document height swing and the scrubber bounce.

Reads each item's on-disk thumb dimensions (PIL .size is header-only, very
fast). Writes the index back atomically. Re-runnable: skips items with `ar`.
"""
import json
import os
import concurrent.futures
from PIL import Image

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IDX = os.path.join(APP, "photo_index.json")


def ar_for(item):
    rel = (item.get("thumb") or item.get("thumb_hq") or "").lstrip("/")
    if not rel:
        return None
    path = os.path.join(APP, rel)
    try:
        with Image.open(path) as im:
            w, h = im.size
        if w > 0 and h > 0:
            return round(w / h, 3)
    except Exception:
        return None
    return None


def main():
    with open(IDX) as f:
        data = json.load(f)
    items = data if isinstance(data, list) else data.get("items", [])
    assert items, "empty index — refusing to write"
    todo = [it for it in items if not it.get("ar")]
    print(f"{len(todo)} of {len(items)} items need an aspect ratio", flush=True)

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(ar_for, it): it for it in todo}
        for fut in concurrent.futures.as_completed(futs):
            ar = fut.result()
            if ar:
                futs[fut]["ar"] = ar
            done += 1
            if done % 5000 == 0:
                print(f"  {done}/{len(todo)}", flush=True)

    got = sum(1 for it in items if it.get("ar"))
    assert got > len(items) * 0.5, f"only {got} aspect ratios — not writing"
    tmp = IDX + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, IDX)
    print(f"wrote {IDX}: {got}/{len(items)} items now have ar", flush=True)


if __name__ == "__main__":
    main()
