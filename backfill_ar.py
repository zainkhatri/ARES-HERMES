"""Backfill `ar` (aspect ratio, w/h rounded to 3dp) into the photo index (photo_db).

Reads dimensions from the generated thumbnails (static/thumbs/<hash>.jpg),
which already encode EXIF orientation (exif_transpose at gen time) and video
poster frames — so they are the cheapest truthful source of display aspect.

Re-runnable: only touches entries missing `ar`; skips entries whose thumb
file does not exist yet (they get filled on the next run, after
convert_thumbs has caught up). Safe to call from scan_incremental().
"""
import os
import sys

from PIL import Image

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

AR_MIN = 0.2   # sanity clamp — anything outside is a corrupt thumb
AR_MAX = 5.0
MAX_ENTRIES = 500000  # loop bound (Power of Ten rule 2)


def thumb_disk_path(thumb_url):
    """Map a /static/... thumb URL to its on-disk path, or None."""
    assert thumb_url is None or isinstance(thumb_url, str)
    if not thumb_url or not thumb_url.startswith("/static/"):
        return None
    rel = thumb_url[len("/static/"):]
    if ".." in rel:
        return None
    return os.path.join(STATIC_DIR, rel)


def read_ar(path):
    """Return w/h from an image file header, or None on any failure."""
    assert isinstance(path, str) and len(path) > 0
    try:
        with Image.open(path) as img:
            w, h = img.size
    except (OSError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    ar = round(w / h, 3)
    if ar < AR_MIN or ar > AR_MAX:
        return None
    return ar


def backfill():
    """Fill missing `ar` fields in-place. Returns (filled, skipped) counts."""
    import photo_db
    entries = photo_db.load_items()
    assert isinstance(entries, list)

    filled = 0
    skipped = 0
    for i, e in enumerate(entries):
        if i >= MAX_ENTRIES:
            break
        if "ar" in e:
            continue
        disk = thumb_disk_path(e.get("thumb")) or thumb_disk_path(e.get("thumb_hq"))
        ar = read_ar(disk) if disk and os.path.exists(disk) else None
        if ar is None:
            skipped += 1
            continue
        e["ar"] = ar
        filled += 1
        if filled % 5000 == 0:
            print(f"  {filled} filled...")

    if filled > 0:
        photo_db.save_items(entries)
    print(f"[backfill_ar] filled={filled} skipped={skipped} total={len(entries)}")
    return filled, skipped


if __name__ == "__main__":
    sys.exit(0 if backfill()[0] >= 0 else 1)
