"""Scan PHOTOS directory and build photo_index.json.

Thumbnails are generated on-demand by app.py (lazy, disk-cached).
This script only extracts dates and builds the index — runs in seconds.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# Camera local timezone used when no UTC offset is embedded in EXIF.
# Use ZoneInfo so DST is applied correctly for each shot date.
_LA_TZ = ZoneInfo("America/Los_Angeles")

# Google Takeout sidecar root: sibling of the year directories.
# Layout: GOOGLE/metadata/<year>/<fname>.supplemental-metadata.json
_GOOGLE_META_DIR = None  # resolved lazily from PHOTOS_ROOT


def _google_meta_dir():
    """Return the Google metadata directory, resolved once."""
    global _GOOGLE_META_DIR
    if _GOOGLE_META_DIR is None:
        # GOOGLE lives directly under the library root.
        # e.g. PHOTOS_ROOT = /mnt/data/PHOTOS -> GOOGLE dir = /mnt/data/PHOTOS/GOOGLE
        candidate = os.path.join(PHOTOS_ROOT, "GOOGLE", "metadata")
        if os.path.isdir(candidate):
            _GOOGLE_META_DIR = candidate
        else:
            _GOOGLE_META_DIR = ""  # sentinel: no sidecar dir found
    return _GOOGLE_META_DIR


def _google_sidecar_ts(filepath):
    """Return photoTakenTime UTC epoch from Google Takeout sidecar, or None.

    Tries three filename variants in order:
      1. <name>.supplemental-metadata.json
      2. <name>.json           (older Takeout format)
      3. truncated-name forms  (Takeout clips long filenames at 46 chars)
    """
    meta_root = _google_meta_dir()
    if not meta_root:
        return None

    # Derive year from path: .../GOOGLE/<year>/file
    parts = filepath.replace("\\", "/").split("/")
    try:
        g_idx = parts.index("GOOGLE")
    except ValueError:
        return None
    if g_idx + 2 >= len(parts):
        return None
    year = parts[g_idx + 1]
    fname = parts[-1]

    year_dir = os.path.join(meta_root, year)
    if not os.path.isdir(year_dir):
        return None

    candidates = [
        fname + ".supplemental-metadata.json",
        fname + ".json",
    ]
    # Takeout truncates filenames to 46 chars before the extension
    stem, ext = os.path.splitext(fname)
    if len(stem) > 46:
        trunc = stem[:46] + ext
        candidates.append(trunc + ".supplemental-metadata.json")
        candidates.append(trunc + ".json")

    for candidate in candidates:
        sidecar = os.path.join(year_dir, candidate)
        if not os.path.exists(sidecar):
            continue
        try:
            with open(sidecar) as sf:
                data = json.load(sf)
            ts_str = data.get("photoTakenTime", {}).get("timestamp")
            if ts_str:
                return float(ts_str)
        except Exception:
            pass
    return None

# NAS-local paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

# Library root: probe known locations (LXC bind-mount first, then ARES host),
# same idiom as app.py's _WORK_CANDIDATES. Env var wins for overrides.
_PHOTOS_ROOT_CANDIDATES = [
    os.environ.get("PHOTOS_ROOT"),
    "/mnt/data/PHOTOS",               # inside LXC 101 (symlink to PROMETHEUS/PHOTOS)
    "/mnt/data/PROMETHEUS/PHOTOS",    # inside LXC 101 (real dir)
    "/mnt/nvme/PROMETHEUS/PHOTOS",    # ARES host
]
PHOTOS_ROOT = next(
    (p for p in _PHOTOS_ROOT_CANDIDATES if p and os.path.isdir(os.path.join(p, "GOOGLE"))),
    "/mnt/data/PHOTOS",
)

# Canonical path prefix used by every existing photo_index.json entry; the app's
# _resolve_photo_path() rewrites it to a real path. New entries keep this form
# so the index stays uniform.
INDEX_PREFIX = "/mnt/data/PHOTOS/PHOTOS"


def _resolve_disk_path(index_path):
    """Map an index path (canonical or legacy prefix) to a real on-disk file, or None."""
    assert isinstance(index_path, str) and index_path, "index_path must be non-empty str"
    if os.path.isfile(index_path):
        return index_path
    for marker in ("/PHOTOS/PHOTOS/", "/PHOTOS/"):
        if marker in index_path:
            rel = index_path.split(marker, 1)[1]
            cand = os.path.join(PHOTOS_ROOT, rel)
            if os.path.isfile(cand):
                return cand
    return None
THUMB_DIR = os.path.join(PROJECT_ROOT, "static", "thumbs")
THUMB_HQ_DIR = os.path.join(PROJECT_ROOT, "static", "thumbs_hq")
INDEX_FILE = os.path.join(PROJECT_ROOT, "photo_index.json")
CONTENT_HASH_FILE = os.path.join(PROJECT_ROOT, "content_hashes.json")

WORKERS = 8

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff", ".tif", ".bmp", ".gif", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}
ALL_EXTS = IMAGE_EXTS | VIDEO_EXTS


def _atomic_write_json(path, data):
    """Write JSON atomically so concurrent readers never see partial writes."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def hash_path(path):
    return hashlib.md5(path.encode()).hexdigest()


def infer_date_from_path(filepath):
    import calendar
    rel = os.path.relpath(filepath, PHOTOS_ROOT)
    parts = rel.split(os.sep)
    for i, part in enumerate(parts):
        if re.match(r"^(19|20)\d{2}$", part):
            year = int(part)
            if i + 1 < len(parts) and re.match(r"^(0[1-9]|1[0-2])$", parts[i + 1]):
                # Month subfolder present — use mid-month
                month = int(parts[i + 1])
                estimated = datetime(year, month, 15, 12, 0, 0)
            else:
                # No month subfolder — spread across the year using filename hash
                # so files get distinct, reproducible dates rather than all July 15
                fname = os.path.basename(filepath)
                h = int(hashlib.md5(fname.encode()).hexdigest()[:8], 16)
                days_in_year = 366 if calendar.isleap(year) else 365
                day_of_year = (h % days_in_year) + 1
                month_days = [31, 29 if calendar.isleap(year) else 28,
                              31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
                cum = 0
                month, day = 7, 1
                for m, d in enumerate(month_days, 1):
                    if day_of_year <= cum + d:
                        month, day = m, day_of_year - cum
                        break
                    cum += d
                hour = (h >> 8) % 24
                minute = (h >> 16) % 60
                estimated = datetime(year, month, day, hour, minute, 0)
            now = datetime.now()
            if estimated > now:
                estimated = now
            return estimated.timestamp()
    return None


def get_media_date(filepath):
    """Extract real date from EXIF/metadata.  Priority order:

    1. EXIF DateTimeOriginal + OffsetTimeOriginal/OffsetTime (timezone-aware UTC).
    2. Google Takeout sidecar photoTakenTime.timestamp (already UTC epoch).
    3. Naive EXIF datetime (no offset) interpreted as America/Los_Angeles.
    4. ffprobe creation_time (videos).
    5. path-inferred date / mtime (last resort).
    """
    assert isinstance(filepath, str) and filepath, "filepath must be a non-empty string"

    # --- Step 1 & 3: exiftool with offset fields ---
    try:
        result = subprocess.run(
            [
                "exiftool",
                "-DateTimeOriginal", "-OffsetTimeOriginal", "-OffsetTime",
                "-CreateDate", "-MediaCreateDate",
                "-s3", "-d", "%Y:%m:%d %H:%M:%S",
                filepath,
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = [ln.strip() for ln in result.stdout.strip().split("\n") if ln.strip()]
            # exiftool -s3 prints values in the order requested.
            # Line 0 = DateTimeOriginal, Line 1 = OffsetTimeOriginal, Line 2 = OffsetTime,
            # Line 3 = CreateDate, Line 4 = MediaCreateDate (only present when found).
            naive_str = None
            offset_str = None
            for ln in lines:
                if re.match(r"\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}", ln):
                    if ln != "0000:00:00 00:00:00":
                        naive_str = ln
                        break
            for ln in lines:
                if re.match(r"[+-]\d{2}:\d{2}", ln):
                    offset_str = ln
                    break
            if naive_str:
                ts = parse_date_with_offset(naive_str, offset_str)
                if ts is not None:
                    return ts
    except Exception:
        pass

    # --- Step 2: Google Takeout sidecar ---
    if "/GOOGLE/" in filepath:
        sidecar_ts = _google_sidecar_ts(filepath)
        if sidecar_ts is not None:
            return sidecar_ts

    # --- Step 4: ffprobe creation_time (video fallback) ---
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_entries", "format_tags=creation_time", filepath,
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            val = data.get("format", {}).get("tags", {}).get("creation_time")
            if val:
                parsed = parse_date(val)
                if parsed:
                    return parsed
    except Exception:
        pass

    # --- Step 5: path inference / mtime ---
    mtime = os.path.getmtime(filepath)
    date_from_path = infer_date_from_path(filepath)

    if date_from_path:
        path_year = datetime.fromtimestamp(date_from_path).year
        mtime_year = datetime.fromtimestamp(mtime).year
        # If the path says the file is from an earlier year than mtime, the file
        # was likely copied to the NAS recently — trust the path-based date.
        # If they agree on year, mtime is fine (Google Takeout sets mtime = shot date).
        if mtime_year > path_year:
            return date_from_path

    return mtime


def parse_date_with_offset(naive_str, offset_str):
    """Convert an EXIF naive datetime string + optional offset string to a UTC epoch.

    If offset_str is provided (e.g. '-07:00'), the datetime is treated as that
    local timezone and converted to UTC correctly.  If no offset is given, the
    datetime is interpreted as America/Los_Angeles (DST-aware) so that photos
    taken in California without embedded timezone info land in the right UTC slot.

    Returns a float UTC epoch, or None on parse failure.
    Two assertions guard inputs; failure returns None rather than raising.
    """
    assert isinstance(naive_str, str), "naive_str must be a string"
    assert offset_str is None or isinstance(offset_str, str), "offset_str must be str or None"

    try:
        dt_naive = datetime.strptime(naive_str.strip(), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None

    if offset_str and re.match(r"[+-]\d{2}:\d{2}", offset_str.strip()):
        # Parse the explicit UTC offset.
        sign = 1 if offset_str[0] == "+" else -1
        parts = offset_str.strip()[1:].split(":")
        offset_minutes = sign * (int(parts[0]) * 60 + int(parts[1]))
        tz = timezone(timedelta(minutes=offset_minutes))
        dt_aware = dt_naive.replace(tzinfo=tz)
    else:
        # No offset: interpret local time as America/Los_Angeles (handles DST via fold=0).
        dt_aware = dt_naive.replace(tzinfo=_LA_TZ)

    return dt_aware.timestamp()


def parse_date(date_str):
    """Parse a date string that may already carry timezone info (ISO-8601 / RFC 3339).

    Used for ffprobe creation_time which arrives as a UTC ISO string.
    For plain EXIF strings (no offset), call parse_date_with_offset instead so
    the correct timezone is applied rather than silently assuming UTC.

    Returns a float UTC epoch, or None on parse failure.
    """
    assert isinstance(date_str, str) and date_str, "date_str must be a non-empty string"

    date_str = date_str.strip()
    # Formats with embedded timezone (already UTC-aware): parse directly.
    for fmt in [
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
    ]:
        try:
            dt = datetime.strptime(date_str[:26], fmt)
            # strptime with %z returns an aware datetime; .timestamp() is correct.
            # strptime with Z suffix returns naive but represents UTC — make it aware.
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    # Fallback for plain date strings: treat as America/Los_Angeles (same as no-offset EXIF).
    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S", "%Y-%m-%d"]:
        try:
            dt_naive = datetime.strptime(date_str[:19], fmt)
            dt_aware = dt_naive.replace(tzinfo=_LA_TZ)
            return dt_aware.timestamp()
        except ValueError:
            continue
    return None


def gen_thumb(filepath, out_path, size, quality, is_video):
    """Generate a single thumbnail.

    Uses PIL (+pillow-heif for HEIC) for images, ffmpeg for videos.
    """
    ext = os.path.splitext(filepath)[1].lower()

    # ── Videos: ffmpeg frame grab ──
    if is_video:
        try:
            for ss in ("1", "0"):
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error",
                     "-i", filepath, "-ss", ss, "-vframes", "1",
                     "-vf", f"scale={size}:-1", "-q:v", str(quality), out_path],
                    capture_output=True, timeout=30
                )
                if os.path.exists(out_path):
                    return True
            return False
        except Exception:
            return False

    # ── All images: PIL (pillow-heif registered so HEIC/HEIF open natively) ──
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    try:
        from PIL import Image, ImageOps
        img = Image.open(filepath)
        img = ImageOps.exif_transpose(img)
        img.thumbnail((size, size * 4), Image.LANCZOS)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        img.convert("RGB").save(out_path, "JPEG", quality=85, optimize=True)
        return os.path.exists(out_path)
    except Exception:
        return False


def _extract_date(job):
    """Extract date for a single file. Runs in worker process."""
    filepath, rel_path, ext = job
    is_video = ext in VIDEO_EXTS
    date = get_media_date(filepath)
    if date is None:
        date = os.path.getmtime(filepath)
    thumb_name = hash_path(rel_path) + ".jpg"
    return {
        "path": os.path.join(INDEX_PREFIX, rel_path),
        "thumb": f"/static/thumbs/{thumb_name}",
        "thumb_hq": f"/static/thumbs_hq/{thumb_name}",
        "date": date,
        "type": "video" if is_video else "image",
    }


def scan():
    print(f"Scanning {PHOTOS_ROOT} ...")
    print(f"Workers: {WORKERS}")
    start = time.time()

    SKIP_DIRS = {"takeouts", "RECYCLE_BIN", "_inbox-snapchat"}

    SKIP_PATTERNS = {"branded", "low-res"}

    all_files = []
    for root, dirs, files in os.walk(PHOTOS_ROOT):
        # Skip named dirs and any dot-directory (includes .vault)
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for fname in files:
            if fname.startswith("._"):
                continue
            fname_lower = fname.lower()
            if any(pat in fname_lower for pat in SKIP_PATTERNS):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext in ALL_EXTS:
                filepath = os.path.join(root, fname)
                all_files.append((filepath, ext))

    total = len(all_files)
    print(f"Found {total} media files in {time.time() - start:.1f}s")
    print(f"Extracting dates from {total} files...")

    jobs = []
    for filepath, ext in all_files:
        rel_path = os.path.relpath(filepath, PHOTOS_ROOT)
        jobs.append((filepath, rel_path, ext))

    entries = []
    done = 0
    failed = 0

    with ProcessPoolExecutor(max_workers=WORKERS) as pool:
        future_to_job = {pool.submit(_extract_date, job): job for job in jobs}
        for future in as_completed(future_to_job):
            try:
                result = future.result()
            except Exception:
                result = None
            if result:
                entries.append(result)
                done += 1
                if done % 2000 == 0:
                    elapsed = time.time() - start
                    rate = done / elapsed if elapsed > 0 else 0
                    remaining = (total - done - failed) / rate if rate > 0 else 0
                    print(f"  {done}/{total} done ({rate:.0f}/s, ~{remaining/60:.1f}m remaining)")
            else:
                failed += 1

    entries.sort(key=lambda x: x["date"], reverse=True)

    _atomic_write_json(INDEX_FILE, entries)

    elapsed = time.time() - start
    print(f"\nDone in {elapsed:.1f}s ({elapsed/60:.1f}m)")
    print(f"  Indexed: {done}  Failed: {failed}")
    print(f"  -> {INDEX_FILE}")


def scan_incremental():
    """Fast incremental scan — only processes files not already in the index.

    Loads existing photo_index.json, finds new/missing files, processes only those,
    then merges and saves. Run this on a cron job for automatic pick-up of new photos.
    """
    print(f"[incremental] Scanning {PHOTOS_ROOT} for new files ...")
    start = time.time()

    SKIP_DIRS = {"takeouts", "RECYCLE_BIN", "_inbox-snapchat"}

    # Load existing index
    existing = []
    if os.path.exists(INDEX_FILE):
        try:
            with open(INDEX_FILE) as f:
                existing = json.load(f)
            print(f"[incremental] Loaded {len(existing)} existing entries.")
        except Exception as e:
            print(f"[incremental] Could not load existing index: {e} — doing full scan.")
            scan()
            return

    # Resolve every index path to its real on-disk location so comparisons work
    # regardless of which prefix form an entry uses.
    known_real = {rp for rp in (_resolve_disk_path(e["path"]) for e in existing) if rp}

    SKIP_PATTERNS = {"branded", "low-res"}

    # Walk filesystem for all media files
    all_files = []
    for root, dirs, files in os.walk(PHOTOS_ROOT):
        # Skip named dirs and any dot-directory (includes .vault)
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for fname in files:
            if fname.startswith("._"):
                continue
            fname_lower = fname.lower()
            if any(pat in fname_lower for pat in SKIP_PATTERNS):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext in ALL_EXTS:
                filepath = os.path.join(root, fname)
                if filepath not in known_real:
                    all_files.append((filepath, ext))

    # Remove index entries for files that no longer exist on disk or match skip patterns
    def _should_keep(e):
        if _resolve_disk_path(e["path"]) is None:
            return False
        fname_lower = os.path.basename(e["path"]).lower()
        return not any(pat in fname_lower for pat in SKIP_PATTERNS)

    still_exist = [e for e in existing if _should_keep(e)]
    removed = len(existing) - len(still_exist)
    if removed:
        print(f"[incremental] Removed {removed} entries for deleted files.")

    if not all_files:
        if removed:
            still_exist.sort(key=lambda x: x["date"], reverse=True)
            _atomic_write_json(INDEX_FILE, still_exist)
            print(f"[incremental] Index updated (deletions only). Done in {time.time()-start:.1f}s")
        else:
            print(f"[incremental] No new files found. Done in {time.time()-start:.1f}s")
        return

    print(f"[incremental] Found {len(all_files)} new file(s) to index.")

    jobs = []
    for filepath, ext in all_files:
        rel_path = os.path.relpath(filepath, PHOTOS_ROOT)
        jobs.append((filepath, rel_path, ext))

    new_entries = []
    failed = 0
    with ProcessPoolExecutor(max_workers=WORKERS) as pool:
        future_to_job = {pool.submit(_extract_date, job): job for job in jobs}
        for future in as_completed(future_to_job):
            try:
                result = future.result()
            except Exception:
                result = None
            if result:
                new_entries.append(result)
            else:
                failed += 1

    merged = still_exist + new_entries
    merged.sort(key=lambda x: x["date"], reverse=True)

    _atomic_write_json(INDEX_FILE, merged)

    # Fill `ar` (aspect ratio) for any entries that don't have it yet —
    # reads thumb headers only, so it's cheap. Entries whose thumbs aren't
    # generated yet are skipped and picked up on the next run.
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from backfill_ar import backfill as _backfill_ar
        _backfill_ar(INDEX_FILE)
    except Exception as e:
        print(f"[incremental] ar backfill skipped: {e}")

    elapsed = time.time() - start
    print(f"[incremental] Done in {elapsed:.1f}s — added {len(new_entries)}, removed {removed}, failed {failed}.")
    print(f"[incremental] Index now has {len(merged)} entries.")


def _hash_file(filepath):
    """Compute SHA256 of a file. Runs in worker process."""
    h = hashlib.sha256()
    try:
        with open(filepath, "rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)  # 1 MB chunks
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest(), filepath
    except Exception:
        return None, filepath


def build_content_hashes():
    """Walk PHOTOS_ROOT and build {sha256: filepath} mapping for dedup."""
    print(f"[hashes] Scanning {PHOTOS_ROOT} for media files ...")
    start = time.time()

    SKIP_DIRS = {"takeouts", "RECYCLE_BIN", "_inbox-snapchat"}
    SKIP_PATTERNS = {"branded", "low-res"}

    all_files = []
    for root, dirs, files in os.walk(PHOTOS_ROOT):
        # Skip named dirs and any dot-directory (includes .vault)
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for fname in files:
            if fname.startswith("._"):
                continue
            fname_lower = fname.lower()
            if any(pat in fname_lower for pat in SKIP_PATTERNS):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext in ALL_EXTS:
                all_files.append(os.path.join(root, fname))

    total = len(all_files)
    print(f"[hashes] Found {total} media files in {time.time() - start:.1f}s")
    print(f"[hashes] Computing SHA256 hashes with {WORKERS} workers ...")

    hashes = {}
    done = 0
    failed = 0

    with ProcessPoolExecutor(max_workers=WORKERS) as pool:
        future_to_path = {pool.submit(_hash_file, fp): fp for fp in all_files}
        for future in as_completed(future_to_path):
            sha, filepath = future.result()
            if sha:
                hashes[sha] = filepath
                done += 1
                if done % 5000 == 0:
                    elapsed = time.time() - start
                    rate = done / elapsed if elapsed > 0 else 0
                    remaining = (total - done - failed) / rate if rate > 0 else 0
                    print(f"  {done}/{total} hashed ({rate:.0f}/s, ~{remaining/60:.1f}m remaining)")
            else:
                failed += 1

    _atomic_write_json(CONTENT_HASH_FILE, hashes)

    elapsed = time.time() - start
    print(f"[hashes] Done in {elapsed:.1f}s ({elapsed/60:.1f}m)")
    print(f"  Hashed: {done}  Failed: {failed}  Unique: {len(hashes)}")
    print(f"  -> {CONTENT_HASH_FILE}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--incremental":
        scan_incremental()
    elif len(sys.argv) > 1 and sys.argv[1] == "--build-hashes":
        build_content_hashes()
    else:
        scan()
