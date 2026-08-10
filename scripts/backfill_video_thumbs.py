#!/usr/bin/env python3
"""Fast video thumbnail backfill. One ffmpeg call per video, PIL downscale for small size."""
import json
import os
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed

PROJECT_ROOT = "/mnt/data/ares-app"
INDEX = f"{PROJECT_ROOT}/photo_index.json"
WORKERS = 3


def process(entry):
    src = entry["path"]
    if not os.path.exists(src):
        return ("missing_src", src)
    thumb_path = PROJECT_ROOT + entry["thumb"]
    hq_path = PROJECT_ROOT + entry["thumb_hq"]
    os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
    os.makedirs(os.path.dirname(hq_path), exist_ok=True)

    have_thumb = os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0
    have_hq = os.path.exists(hq_path) and os.path.getsize(hq_path) > 0
    if have_thumb and have_hq:
        return ("skip", src)

    # One ffmpeg call: grab frame at 1s (fallback 0s), scale to 800px, write HQ
    for ss in ("1", "0.5", "0"):
        try:
            r = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error",
                 "-ss", ss, "-i", src, "-vframes", "1",
                 "-vf", "scale=800:-1", "-q:v", "2", hq_path],
                capture_output=True, timeout=25,
            )
            if os.path.exists(hq_path) and os.path.getsize(hq_path) > 0:
                break
        except Exception:
            pass
    if not (os.path.exists(hq_path) and os.path.getsize(hq_path) > 0):
        return ("fail", src)

    # Downscale HQ to small thumb with PIL (much faster than second ffmpeg)
    if not have_thumb:
        try:
            from PIL import Image
            img = Image.open(hq_path)
            img.thumbnail((475, 475 * 4), Image.LANCZOS)
            img.convert("RGB").save(thumb_path, "JPEG", quality=80, optimize=True)
        except Exception:
            return ("partial", src)
    return ("ok", src)


def main():
    with open(INDEX) as f:
        photos = json.load(f)
    videos = [p for p in photos if p.get("type") == "video"]
    print(f"Total videos: {len(videos)}", flush=True)
    todo = []
    for v in videos:
        t = PROJECT_ROOT + v["thumb"]
        h = PROJECT_ROOT + v["thumb_hq"]
        if not (os.path.exists(t) and os.path.getsize(t) > 0) or \
           not (os.path.exists(h) and os.path.getsize(h) > 0):
            todo.append(v)
    print(f"Needing thumbnails: {len(todo)}", flush=True)
    if not todo:
        return
    counts = {"ok": 0, "fail": 0, "skip": 0, "missing_src": 0, "partial": 0}
    done = 0
    import time
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(process, v) for v in todo]
        for fut in as_completed(futs):
            status, _ = fut.result()
            counts[status] = counts.get(status, 0) + 1
            done += 1
            if done % 50 == 0:
                elapsed = time.time() - t0
                rate = done / elapsed
                eta = (len(todo) - done) / rate / 60
                print(f"  {done}/{len(todo)}  {rate:.1f}/s  ok={counts['ok']} fail={counts['fail']}  ETA {eta:.0f}m", flush=True)
    print(f"\nDone: {counts}", flush=True)


if __name__ == "__main__":
    main()
