#!/usr/bin/env python3
"""
Convert all ARES thumbnails JPG -> WebP in-place, resumable, safe.

Phases:
  1 = convert JPG -> WebP (skip if webp already exists)
  2 = update photo_index.json + related to point to .webp
  3 = delete JPG files that have a valid WebP counterpart

Usage:
  python3 convert_thumbs.py 1      # just convert
  python3 convert_thumbs.py 1 2 3  # full pipeline
  python3 convert_thumbs.py 2 3    # post-conversion cleanup only
"""
import os
import sys
import re
import glob
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from PIL import Image

ROOT = '/mnt/data/ares-app/static'
DIRS = [
    (f'{ROOT}/thumbs',         80),  # grid — tiny, can be lower quality
    (f'{ROOT}/thumbs_hq',      82),  # album previews — mid
    (f'{ROOT}/thumbs_preview', 85),  # lightbox — higher quality
]
INDEX_FILES = [
    '/mnt/data/ares-app/photo_index.json',
    '/mnt/data/ares-app/gpt_index.json',
    '/mnt/data/ares-app/content_hashes.json',
]
WORKERS = 4


def convert_one(jpg_path, quality):
    webp_path = jpg_path[:-4] + '.webp'
    tmp_path = webp_path + '.tmp'
    if os.path.exists(webp_path) and os.path.getsize(webp_path) > 100:
        return 'exists'
    try:
        with Image.open(jpg_path) as img:
            if img.mode not in ('RGB', 'RGBA'):
                img = img.convert('RGB')
            img.save(tmp_path, 'webp', quality=quality, method=4)
        if os.path.getsize(tmp_path) < 100:
            os.remove(tmp_path)
            return 'tiny'
        os.rename(tmp_path, webp_path)
        return 'ok'
    except Exception as e:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        return f'err:{type(e).__name__}'


def phase1_convert():
    for dir_path, quality in DIRS:
        if not os.path.isdir(dir_path):
            print(f"[skip] {dir_path} — not found")
            continue
        files = glob.glob(f'{dir_path}/*.jpg')
        if not files:
            print(f"[skip] {dir_path} — no JPGs")
            continue
        print(f"[phase 1] {dir_path}: {len(files)} files  q={quality}")
        start = time.time()
        done = 0
        errs = 0
        already = 0
        with ProcessPoolExecutor(max_workers=WORKERS) as ex:
            futures = [ex.submit(convert_one, f, quality) for f in files]
            for fut in as_completed(futures):
                r = fut.result()
                done += 1
                if r == 'exists':
                    already += 1
                elif r.startswith('err') or r == 'tiny':
                    errs += 1
                if done % 500 == 0 or done == len(files):
                    elapsed = time.time() - start
                    rate = done / elapsed if elapsed > 0 else 0
                    eta = (len(files) - done) / rate / 60 if rate > 0 else 0
                    print(f"  {done}/{len(files)}  converted={done-already-errs} skipped={already} errors={errs}  {rate:.1f}/s  eta={eta:.1f}min")
        elapsed = (time.time() - start) / 60
        print(f"  [done] {dir_path}  total_time={elapsed:.1f}min  errors={errs}")


def phase2_update_index():
    pattern = re.compile(r'(/thumbs(?:_hq|_preview)?/[a-f0-9]+)\.jpg')
    for idx in INDEX_FILES:
        if not os.path.exists(idx):
            print(f"[skip] {idx} — not found")
            continue
        with open(idx, 'r') as f:
            data = f.read()
        new_data = pattern.sub(r'\1.webp', data)
        if new_data == data:
            print(f"[skip] {idx} — no thumb refs")
            continue
        # backup then write
        with open(idx + '.bak', 'w') as f:
            f.write(data)
        with open(idx, 'w') as f:
            f.write(new_data)
        changes = data.count('/thumbs') - new_data.count('.jpg')
        print(f"[phase 2] {idx}: updated (~{changes} thumb refs)")


def phase3_delete_jpgs():
    for dir_path, _ in DIRS:
        if not os.path.isdir(dir_path):
            continue
        jpgs = glob.glob(f'{dir_path}/*.jpg')
        deleted = 0
        skipped = 0
        for jpg in jpgs:
            webp = jpg[:-4] + '.webp'
            if os.path.exists(webp) and os.path.getsize(webp) > 100:
                os.remove(jpg)
                deleted += 1
            else:
                skipped += 1
        print(f"[phase 3] {dir_path}: deleted {deleted}  kept {skipped} (no webp counterpart)")


if __name__ == '__main__':
    phases = sys.argv[1:] or ['1']
    if '1' in phases:
        phase1_convert()
    if '2' in phases:
        phase2_update_index()
    if '3' in phases:
        phase3_delete_jpgs()
    print('[all done]')
