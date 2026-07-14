#!/usr/bin/env python3
"""Backfill `c` (average color, 6-hex) onto every photo_index item so the grid
paints a real-color placeholder per tile instead of gray while thumbnails load.
The frontend (thumbHTML) already consumes item.c — this just generates the data.

Reads the already-on-disk 475px thumb for each item (fast: decode + 1x1 resize),
writes the index back atomically. Re-runnable: skips items that already have `c`.
"""
import os
import sys
import concurrent.futures
from PIL import Image

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)
import photo_db


def color_for(item):
    """Average color hex for an item's thumb, or None if unreadable."""
    rel = (item.get("thumb") or item.get("thumb_hq") or "").lstrip("/")
    if not rel:
        return None
    path = os.path.join(APP, rel)
    try:
        with Image.open(path) as im:
            r, g, b = im.convert("RGB").resize((1, 1), Image.BILINEAR).getpixel((0, 0))
        return "%02x%02x%02x" % (r, g, b)
    except Exception:
        return None


def main():
    items = photo_db.load_items()
    assert items, "empty index — refusing to write"
    todo = [it for it in items if not it.get("c")]
    print(f"{len(todo)} of {len(items)} items need a color", flush=True)

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(color_for, it): it for it in todo}
        for fut in concurrent.futures.as_completed(futs):
            c = fut.result()
            if c:
                futs[fut]["c"] = c
            done += 1
            if done % 5000 == 0:
                print(f"  {done}/{len(todo)}", flush=True)

    got = sum(1 for it in items if it.get("c"))
    assert got > len(items) * 0.5, f"only {got} colors — something is wrong, not writing"
    photo_db.save_items(items)
    print(f"wrote {photo_db.DB_PATH}: {got}/{len(items)} items now have a color", flush=True)


if __name__ == "__main__":
    main()
