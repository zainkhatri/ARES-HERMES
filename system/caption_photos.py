#!/usr/bin/env python3
"""Caption photos with llava via Ollama, store in ai_data/captions.json.

Reads thumb images, asks llava to describe them, writes captions keyed by
thumb MD5 (the filename stem). Safe to kill and restart — already-captioned
photos are skipped.

Usage:
  python3 caption_photos.py [--limit N] [--batch N] [--model MODEL]

Options:
  --limit  N      stop after captioning N new photos (default: all)
  --batch  N      save every N captions (default: 50)
  --model  MODEL  ollama model (default: llava)
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.request

_DIR = os.path.dirname(os.path.abspath(__file__))
_DASH = os.path.dirname(_DIR)
_AI   = os.path.join(_DASH, "ai_data")
_CAPS = os.path.join(_AI, "captions.json")
_THUMBS = os.path.join(_DASH, "static", "thumbs")
_OLLAMA = "http://127.0.0.1:11434"

sys.path.insert(0, _DASH)
import photo_db


def _load_captions():
    if os.path.exists(_CAPS):
        with open(_CAPS) as f:
            return json.load(f)
    return {}


def _save_captions(caps):
    tmp = _CAPS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(caps, f, separators=(",", ":"))
    os.replace(tmp, _CAPS)


def _thumb_key(item):
    thumb = item.get("thumb", "") or item.get("thumb_hq", "")
    if not thumb:
        return None
    stem = os.path.basename(thumb)
    return os.path.splitext(stem)[0]


def _thumb_path(key):
    for ext in (".jpg", ".webp", ".jpeg", ".png"):
        p = os.path.join(_THUMBS, key + ext)
        if os.path.exists(p):
            return p
    return None


def _caption(img_path, model):
    with open(img_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    payload = json.dumps({
        "model": model,
        "prompt": (
            "Describe this photo in one or two sentences. "
            "Be specific: mention people, setting, activity, objects, and mood. "
            "Do not say 'the image shows' — just describe directly."
        ),
        "images": [b64],
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        _OLLAMA + "/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read())
    return resp.get("response", "").strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--model", default="llava")
    args = ap.parse_args()

    # assert ollama is reachable
    try:
        urllib.request.urlopen(_OLLAMA + "/api/tags", timeout=5)
    except Exception as e:
        print(f"ERROR: Ollama not reachable at {_OLLAMA}: {e}", flush=True)
        sys.exit(1)

    caps = _load_captions()
    items = [it for it in photo_db.load_items() if it.get("type") == "image"]
    items.sort(key=lambda x: x.get("date", 0), reverse=True)  # newest first

    done = 0
    skipped = 0
    errors = 0

    for it in items:
        if args.limit and done >= args.limit:
            break

        key = _thumb_key(it)
        if not key or key in caps:
            skipped += 1
            continue

        thumb = _thumb_path(key)
        if not thumb:
            skipped += 1
            continue

        try:
            t0 = time.monotonic()
            text = _caption(thumb, args.model)
            elapsed = time.monotonic() - t0
            caps[key] = text
            done += 1
            print(f"[{done}] {os.path.basename(it['path'])} ({elapsed:.1f}s): {text[:80]}", flush=True)
        except Exception as e:
            errors += 1
            print(f"ERR {key}: {e}", flush=True)

        if done % args.batch == 0:
            _save_captions(caps)
            print(f"  saved {len(caps)} captions", flush=True)

    _save_captions(caps)
    total = len(caps)
    print(f"\nDone. new={done} skipped={skipped} errors={errors} total_in_file={total}", flush=True)


if __name__ == "__main__":
    main()
