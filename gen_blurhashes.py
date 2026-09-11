#!/usr/bin/env python3
"""Generate a {thumb_hash: blurhash} map for every photo, from the existing 475px grid
thumbs (no original reads). Incremental: keeps existing entries, only encodes new hashes.
Output: blurhashes.json next to the app, served by /api/photos/blurhashes.

Run:  python3 gen_blurhashes.py
"""
import os, sys, json, glob, sqlite3, time
from concurrent.futures import ThreadPoolExecutor
import blurhash
from PIL import Image

APP = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(APP, "blurhashes.json")
THUMBS = os.path.join(APP, "static", "thumbs")

def photo_hashes():
    """Thumb-file hashes for non-video items in the index."""
    con = sqlite3.connect(os.path.join(APP, "photo_index.db"))
    out = []
    for _, item in con.execute("SELECT path,item FROM photos"):
        try:
            d = json.loads(item)
        except Exception:
            continue
        if d.get("type") == "video":
            continue
        t = d.get("thumb", "")
        h = t.rsplit("/", 1)[-1].replace(".jpg", "")
        if h:
            out.append(h)
    return out

def encode(h):
    f = os.path.join(THUMBS, h + ".jpg")
    if not os.path.exists(f):
        return h, None
    try:
        im = Image.open(f).convert("RGB")
        im.thumbnail((32, 32))
        w, hh = im.size
        px = list(im.getdata())
        rows = [[list(px[y * w + x]) for x in range(w)] for y in range(hh)]
        return h, blurhash.encode(rows, 4, 3)
    except Exception:
        return h, None

def main():
    existing = {}
    if os.path.exists(OUT):
        try:
            existing = json.load(open(OUT))
        except Exception:
            existing = {}
    hashes = photo_hashes()
    todo = [h for h in hashes if h not in existing]
    print(f"photos={len(hashes)} already={len(existing)} to-encode={len(todo)}", flush=True)
    t0 = time.time(); done = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for h, bh in ex.map(encode, todo):
            if bh:
                existing[h] = bh
            done += 1
            if done % 2000 == 0:
                print(f"  {done}/{len(todo)}  {(time.time()-t0):.0f}s", flush=True)
    # Prune entries whose photo left the index.
    keep = set(hashes)
    pruned = {h: b for h, b in existing.items() if h in keep}
    tmp = OUT + ".part"
    json.dump(pruned, open(tmp, "w"), separators=(",", ":"))
    os.replace(tmp, OUT)
    print(f"wrote {len(pruned)} blurhashes to {OUT} in {(time.time()-t0):.0f}s "
          f"({os.path.getsize(OUT)//1024} KB)", flush=True)

if __name__ == "__main__":
    main()
