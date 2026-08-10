"""In-place date-fix for photo_index.json.

Reads every entry, computes the correct UTC epoch using the updated priority
logic, rewrites only the 'date' field, preserves all other fields, then
writes the index atomically.  Backs up the original first.

Priority per file:
  1. EXIF DateTimeOriginal + OffsetTimeOriginal/OffsetTime  ->  aware UTC
  2. Google Takeout sidecar photoTakenTime.timestamp        ->  already UTC epoch
  3. Naive EXIF datetime (no offset)  ->  interpret as America/Los_Angeles
  4. Keep existing date (unchanged)

Run on the HOST (not inside LXC) because:
  - PIL reads JPEG/PNG EXIF directly from host paths
  - HEIC and videos are processed by a single batch exiftool run inside LXC 101
  - Google sidecars are read directly from the host filesystem

Usage:
  /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/clip-gpu-venv/bin/python fix_dates.py

Safety:
  - Backs up photo_index.json before any write
  - Writes atomically (tmp + os.replace)
  - Does NOT touch thumbnails
  - Does NOT restart app (app reloads index via mtime cache)
"""

import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# ── Constants ──────────────────────────────────────────────────────────────────

INDEX_FILE = "/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/photo_index.json"
BACKUP_FILE = f"{INDEX_FILE}.bak-datefix-{int(time.time())}"

# Host path root: index path prefix -> host prefix
INDEX_PREFIX = "/mnt/data/PHOTOS/PHOTOS/"
HOST_PREFIX  = "/mnt/nvme/PROMETHEUS/PHOTOS/"

# Google sidecar root on host
GOOGLE_META_ROOT = "/mnt/nvme/PROMETHEUS/PHOTOS/GOOGLE/metadata"

# America/Los_Angeles for naive EXIF interpretation
_LA_TZ = ZoneInfo("America/Los_Angeles")

# PIL-readable extensions (JPEG/PNG; no HEIC on host without pillow_heif)
PIL_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
# Need exiftool (via LXC) for these
LXC_EXTS = {".heic", ".heif", ".mov", ".mp4", ".avi", ".mkv", ".m4v", ".tiff", ".tif", ".bmp"}

WORKERS = 8

# ── PIL EXIF tag IDs ───────────────────────────────────────────────────────────
TAG_DATETIME_ORIGINAL = 36867   # DateTimeOriginal
TAG_OFFSET_TIME_ORIG  = 36881   # OffsetTimeOriginal
TAG_OFFSET_TIME       = 36880   # OffsetTime
TAG_CREATE_DATE       = 36868   # DateTimeDigitized (fallback)


# ── Helper functions ───────────────────────────────────────────────────────────

def index_to_host(idx_path):
    """Translate an index LXC path to the actual host filesystem path."""
    assert isinstance(idx_path, str) and idx_path, "idx_path must be a non-empty string"
    return idx_path.replace(INDEX_PREFIX, HOST_PREFIX)


def parse_naive_exif_with_tz(naive_str, offset_str=None):
    """Convert EXIF 'YYYY:MM:DD HH:MM:SS' + optional '+HH:MM' offset to UTC epoch.

    No offset -> interpret as America/Los_Angeles (DST-aware).
    Returns float epoch or None.
    """
    assert isinstance(naive_str, str), "naive_str must be a string"

    try:
        dt_naive = datetime.strptime(naive_str.strip(), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None

    if offset_str and re.match(r"[+-]\d{2}:\d{2}", offset_str.strip()):
        sign = 1 if offset_str[0] == "+" else -1
        h, m = offset_str.strip()[1:].split(":")
        tz = timezone(timedelta(minutes=sign * (int(h) * 60 + int(m))))
        return dt_naive.replace(tzinfo=tz).timestamp()

    return dt_naive.replace(tzinfo=_LA_TZ).timestamp()


def google_sidecar_ts(idx_path):
    """Return photoTakenTime UTC epoch from Google Takeout sidecar, or None.

    Sidecar layout: GOOGLE/metadata/<year>/<fname>.supplemental-metadata.json
    Also tries .json and truncated-name variants.
    """
    assert "/GOOGLE/" in idx_path, "idx_path must contain /GOOGLE/"

    parts = idx_path.split("/")
    try:
        g_idx = parts.index("GOOGLE")
    except ValueError:
        return None
    if g_idx + 2 >= len(parts):
        return None

    year = parts[g_idx + 1]
    fname = parts[-1]
    year_dir = os.path.join(GOOGLE_META_ROOT, year)
    if not os.path.isdir(year_dir):
        return None

    stem, ext = os.path.splitext(fname)
    candidates = [
        fname + ".supplemental-metadata.json",
        fname + ".json",
    ]
    if len(stem) > 46:
        trunc = stem[:46] + ext
        candidates.append(trunc + ".supplemental-metadata.json")
        candidates.append(trunc + ".json")

    for cand in candidates:
        sidecar = os.path.join(year_dir, cand)
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


def pil_get_date(host_path):
    """Read DateTimeOriginal + OffsetTimeOriginal from JPEG/PNG via PIL.

    Returns float UTC epoch or None.
    Runs in worker processes via ProcessPoolExecutor.
    """
    assert isinstance(host_path, str) and host_path, "host_path must be non-empty"

    try:
        from PIL import Image
        img = Image.open(host_path)
        exif = img._getexif()
        if not exif:
            return None
        naive_str  = exif.get(TAG_DATETIME_ORIGINAL) or exif.get(TAG_CREATE_DATE)
        offset_str = exif.get(TAG_OFFSET_TIME_ORIG) or exif.get(TAG_OFFSET_TIME)
        if not naive_str:
            return None
        return parse_naive_exif_with_tz(naive_str, offset_str)
    except Exception:
        return None


def _pil_worker(job):
    """Worker shim: (idx_path, host_path) -> (idx_path, new_ts or None)."""
    idx_path, host_path = job
    return idx_path, pil_get_date(host_path)


def build_sidecar_map():
    """Pre-load all Google sidecar timestamps into memory (fast dict lookup).

    Returns dict: {(year, fname): float_epoch}
    """
    result = {}
    if not os.path.isdir(GOOGLE_META_ROOT):
        return result

    for year in os.listdir(GOOGLE_META_ROOT):
        year_dir = os.path.join(GOOGLE_META_ROOT, year)
        if not os.path.isdir(year_dir):
            continue
        for sname in os.listdir(year_dir):
            if not (sname.endswith(".supplemental-metadata.json") or sname.endswith(".json")):
                continue
            # Strip the sidecar suffix to recover the media filename
            if sname.endswith(".supplemental-metadata.json"):
                media_fname = sname[: -len(".supplemental-metadata.json")]
            else:
                media_fname = sname[: -len(".json")]
            sidecar = os.path.join(year_dir, sname)
            try:
                with open(sidecar) as sf:
                    data = json.load(sf)
                ts_str = data.get("photoTakenTime", {}).get("timestamp")
                if ts_str:
                    result[(year, media_fname)] = float(ts_str)
            except Exception:
                pass
    return result


def sidecar_ts_from_map(sidecar_map, idx_path):
    """Look up a GOOGLE entry in the preloaded sidecar map.

    Tries exact fname then 46-char-truncated form.
    Returns float epoch or None.
    """
    parts = idx_path.split("/")
    try:
        g_idx = parts.index("GOOGLE")
    except ValueError:
        return None
    if g_idx + 2 >= len(parts):
        return None
    year = parts[g_idx + 1]
    fname = parts[-1]
    ts = sidecar_map.get((year, fname))
    if ts is not None:
        return ts
    stem, ext = os.path.splitext(fname)
    if len(stem) > 46:
        trunc = stem[:46] + ext
        ts = sidecar_map.get((year, trunc))
    return ts


def run_exiftool_batch_in_lxc(lxc_paths):
    """Run exiftool in LXC 101 over a list of paths, return dict idx->result.

    Returns dict: {lxc_path: {"DateTimeOriginal": ..., "OffsetTimeOriginal": ...}}
    Runs as a single pct exec call with a temp filelist in /tmp.
    """
    assert isinstance(lxc_paths, list), "lxc_paths must be a list"
    if not lxc_paths:
        return {}

    filelist = "/tmp/fix_dates_filelist.txt"
    with open(filelist, "w") as fl:
        for p in lxc_paths:
            fl.write(p + "\n")

    out_file = "/tmp/fix_dates_exif.json"
    cmd = [
        "pct", "exec", "101", "--",
        "bash", "-c",
        f"exiftool -json -q -DateTimeOriginal -OffsetTimeOriginal -OffsetTime "
        f"-CreateDate -MediaCreateDate "
        f"-d '%Y:%m:%d %H:%M:%S' "
        f"-@ /tmp/fix_dates_filelist.txt > /tmp/fix_dates_exif.json 2>/dev/null; "
        f"echo done",
    ]
    try:
        subprocess.run(cmd, timeout=1800, check=True)
    except subprocess.CalledProcessError as e:
        print(f"  [warn] exiftool batch returned non-zero: {e}")

    # Read result from the shared filesystem (filelist is in /tmp on host which LXC can't see,
    # but the OUTPUT file at /tmp/fix_dates_exif.json is on LXC's /tmp -- read via pct exec cat)
    try:
        cat_result = subprocess.run(
            ["pct", "exec", "101", "--", "cat", "/tmp/fix_dates_exif.json"],
            capture_output=True, text=True, timeout=60,
        )
        if cat_result.returncode != 0 or not cat_result.stdout.strip():
            return {}
        exif_list = json.loads(cat_result.stdout)
    except Exception as e:
        print(f"  [warn] failed to read exiftool JSON output: {e}")
        return {}

    result = {}
    for item in exif_list:
        src = item.get("SourceFile", "")
        dto = item.get("DateTimeOriginal") or item.get("CreateDate") or item.get("MediaCreateDate")
        off = item.get("OffsetTimeOriginal") or item.get("OffsetTime")
        result[src] = {"DateTimeOriginal": dto, "OffsetTimeOriginal": off}
    return result


def lxc_path_for(idx_path):
    """Translate index path to LXC-accessible path.

    Index: /mnt/data/PHOTOS/PHOTOS/<subdir>/<rest>
    LXC:   /mnt/data/PHOTOS/<subdir>/<rest>
    """
    return idx_path.replace("/mnt/data/PHOTOS/PHOTOS/", "/mnt/data/PHOTOS/")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    assert os.path.exists(INDEX_FILE), f"Index not found: {INDEX_FILE}"

    print(f"Loading {INDEX_FILE} ...")
    with open(INDEX_FILE) as f:
        index = json.load(f)
    total = len(index)
    print(f"Loaded {total} entries.")

    # ── Backup ──────────────────────────────────────────────────────────────
    print(f"Backing up to {BACKUP_FILE} ...")
    tmp_bak = BACKUP_FILE + ".tmp"
    with open(tmp_bak, "w") as f:
        json.dump(index, f)
    os.replace(tmp_bak, BACKUP_FILE)
    print("Backup written.")

    # ── Pre-load Google sidecar map ──────────────────────────────────────────
    print("Pre-loading Google sidecar map ...")
    sidecar_map = build_sidecar_map()
    print(f"  {len(sidecar_map)} sidecar entries loaded.")

    # ── Snapshot year distribution BEFORE fix ────────────────────────────────
    year_before = Counter()
    for e in index:
        dt = datetime.fromtimestamp(e["date"], tz=_LA_TZ)
        year_before[dt.year] += 1

    # ── Build work lists ─────────────────────────────────────────────────────
    # Map idx_path -> entry for O(1) update
    entry_map = {e["path"]: e for e in index}

    pil_jobs = []    # (idx_path, host_path)
    lxc_jobs = []    # idx_path (for exiftool in LXC)
    google_paths = []  # idx_path (GOOGLE entries — sidecar lookup only)

    for e in index:
        idx_path  = e["path"]
        host_path = index_to_host(idx_path)
        ext = os.path.splitext(idx_path)[1].lower()

        if not os.path.exists(host_path):
            continue  # file missing on host; keep existing date

        if ext in PIL_EXTS:
            pil_jobs.append((idx_path, host_path))
        elif ext in LXC_EXTS:
            lxc_jobs.append(idx_path)

        if "/GOOGLE/" in idx_path:
            google_paths.append(idx_path)

    print(f"Work split: {len(pil_jobs)} PIL, {len(lxc_jobs)} LXC-exiftool, {len(google_paths)} Google-sidecar")

    # ── Phase 1: PIL batch (JPEG/PNG) ────────────────────────────────────────
    print(f"\nPhase 1: PIL EXIF on {len(pil_jobs)} files ({WORKERS} workers) ...")
    pil_results = {}  # idx_path -> new_ts
    done = 0
    with ProcessPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(_pil_worker, job): job for job in pil_jobs}
        for fut in as_completed(futures):
            try:
                idx_path, ts = fut.result()
                if ts is not None:
                    pil_results[idx_path] = ts
            except Exception:
                pass
            done += 1
            if done % 5000 == 0:
                print(f"  PIL: {done}/{len(pil_jobs)}")
    print(f"  PIL done: {len(pil_results)} dates extracted.")

    # ── Phase 2: LXC exiftool batch (HEIC / video) ───────────────────────────
    lxc_results = {}   # idx_path -> {"DateTimeOriginal": ..., "OffsetTimeOriginal": ...}
    if lxc_jobs:
        print(f"\nPhase 2: exiftool batch in LXC 101 for {len(lxc_jobs)} files ...")
        # Translate to LXC paths for exiftool
        lxc_path_map = {}  # lxc_path -> idx_path
        lxc_path_list = []
        for idx_path in lxc_jobs:
            lp = lxc_path_for(idx_path)
            lxc_path_map[lp] = idx_path
            lxc_path_list.append(lp)

        # Write filelist to shared location so LXC can read it
        shared_filelist = "/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/_fix_dates_lxc_filelist.txt"
        lxc_filelist    = "/mnt/data/PROJECTS/ARES-DASHBOARD/_fix_dates_lxc_filelist.txt"
        lxc_outfile     = "/mnt/data/PROJECTS/ARES-DASHBOARD/_fix_dates_exif_out.json"
        host_outfile    = "/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/_fix_dates_exif_out.json"

        with open(shared_filelist, "w") as fl:
            for lp in lxc_path_list:
                fl.write(lp + "\n")

        cmd = [
            "pct", "exec", "101", "--",
            "bash", "-c",
            f"exiftool -json -q -DateTimeOriginal -OffsetTimeOriginal -OffsetTime "
            f"-CreateDate -MediaCreateDate "
            f"-d '%Y:%m:%d %H:%M:%S' "
            f"-@ '{lxc_filelist}' > '{lxc_outfile}' 2>/dev/null; echo done",
        ]
        print("  Running exiftool in LXC (this may take several minutes) ...")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            if result.returncode != 0:
                print(f"  [warn] pct exec returned {result.returncode}: {result.stderr[:200]}")
        except subprocess.TimeoutExpired:
            print("  [warn] exiftool batch timed out")
        except Exception as ex:
            print(f"  [warn] pct exec failed: {ex}")

        if os.path.exists(host_outfile):
            try:
                with open(host_outfile) as jf:
                    exif_list = json.load(jf)
                print(f"  exiftool returned {len(exif_list)} results.")
                for item in exif_list:
                    src = item.get("SourceFile", "")
                    dto = (item.get("DateTimeOriginal") or
                           item.get("CreateDate") or
                           item.get("MediaCreateDate"))
                    off = item.get("OffsetTimeOriginal") or item.get("OffsetTime")
                    if src in lxc_path_map:
                        idx_p = lxc_path_map[src]
                        if dto and dto != "0000:00:00 00:00:00":
                            lxc_results[idx_p] = {"DateTimeOriginal": dto, "OffsetTimeOriginal": off}
            except Exception as ex:
                print(f"  [warn] failed to parse exiftool JSON: {ex}")
            # Cleanup temp files
            try:
                os.remove(host_outfile)
                os.remove(shared_filelist)
            except Exception:
                pass
        else:
            print("  [warn] exiftool output file not found — HEIC/video dates unchanged.")

    # ── Phase 3: Apply all date corrections ──────────────────────────────────
    print("\nPhase 3: Applying date corrections ...")
    changed = 0
    unchanged = 0
    errors = 0
    shift_histogram = Counter()   # bucket -> count
    google_from_sidecar = 0

    for e in index:
        idx_path = e["path"]
        old_ts   = e["date"]
        new_ts   = None

        ext = os.path.splitext(idx_path)[1].lower()

        # Priority 1: PIL result (JPEG/PNG)
        if idx_path in pil_results:
            new_ts = pil_results[idx_path]

        # Priority 1 (for HEIC/video): exiftool result via LXC
        if new_ts is None and idx_path in lxc_results:
            info = lxc_results[idx_path]
            dto = info.get("DateTimeOriginal")
            off = info.get("OffsetTimeOriginal")
            if dto:
                new_ts = parse_naive_exif_with_tz(dto, off)

        # Priority 2: Google sidecar (overrides for GOOGLE files if sidecar is available)
        # Apply sidecar when:
        #   a) No EXIF date found (new_ts is None), OR
        #   b) File is in GOOGLE/ and a sidecar exists (sidecar is more authoritative for Takeout)
        if "/GOOGLE/" in idx_path:
            sc_ts = sidecar_ts_from_map(sidecar_map, idx_path)
            if sc_ts is not None:
                new_ts = sc_ts
                google_from_sidecar += 1

        if new_ts is None:
            unchanged += 1
            continue

        # Sanity: reject timestamps in the future or before 1970
        now_ts = time.time()
        if new_ts <= 0 or new_ts > now_ts + 86400:
            errors += 1
            continue

        if abs(new_ts - old_ts) < 2:
            unchanged += 1
            continue

        # Bucket shift magnitude
        shift_h = round((new_ts - old_ts) / 3600)
        shift_histogram[shift_h] += 1

        e["date"] = new_ts
        changed += 1

    print(f"  Changed: {changed}  Unchanged: {unchanged}  Errors: {errors}")
    print(f"  Google sidecar applied: {google_from_sidecar}")

    # ── Shift histogram ───────────────────────────────────────────────────────
    print("\nShift histogram (hours -> count):")
    for shift_h in sorted(shift_histogram.keys()):
        bar = "#" * min(40, shift_histogram[shift_h] // max(1, changed // 80))
        print(f"  {shift_h:+5d}h : {shift_histogram[shift_h]:6d}  {bar}")

    # ── Year distribution AFTER fix ───────────────────────────────────────────
    year_after = Counter()
    for e in index:
        dt = datetime.fromtimestamp(e["date"], tz=_LA_TZ)
        year_after[dt.year] += 1

    all_years = sorted(set(year_before) | set(year_after))
    print("\nPer-year count: before -> after")
    for yr in all_years:
        b = year_before.get(yr, 0)
        a = year_after.get(yr, 0)
        diff = a - b
        marker = f"  ({diff:+d})" if diff else ""
        print(f"  {yr}: {b:5d} -> {a:5d}{marker}")

    # ── Write corrected index atomically ──────────────────────────────────────
    index.sort(key=lambda x: x["date"], reverse=True)
    tmp_out = INDEX_FILE + ".tmp"
    with open(tmp_out, "w") as f:
        json.dump(index, f)
    os.replace(tmp_out, INDEX_FILE)
    print(f"\nWrote corrected index ({len(index)} entries) -> {INDEX_FILE}")
    print(f"Backup: {BACKUP_FILE}")


if __name__ == "__main__":
    main()
