"""One-time migration of legacy PHOTOS/RECYCLE_BIN into the current .ares-trash bin."""
import os, json, hashlib, shutil
from datetime import datetime

PHOTO_ROOT_CONTAINER = "/mnt/data/PROMETHEUS/PHOTOS"

def reconstruct_original_path(subpath: str, photo_root: str = PHOTO_ROOT_CONTAINER) -> str:
    return f"{photo_root}/{subpath}"

def legacy_thumb_basenames(subpath: str) -> tuple[str, str]:
    h = hashlib.md5(subpath.encode()).hexdigest()
    return f"thumbs_hq_{h}.jpg", f"thumbs_{h}.jpg"

def build_meta(original_path: str, trash_name: str, mtime: float, size: int) -> dict:
    return {
        "original_path": original_path,
        "trash_name": trash_name,
        "trashed_at": datetime.fromtimestamp(mtime).isoformat(),
        "size": size,
    }

def unique_trash_name(dest_dir: str, mtime: float, basename: str) -> str:
    stamp = datetime.fromtimestamp(mtime).strftime("%Y%m%d_%H%M%S")
    name = f"{stamp}_{basename}"
    n = 1
    while os.path.exists(os.path.join(dest_dir, name)):
        stem, ext = os.path.splitext(basename)
        name = f"{stamp}_{stem}_{n}{ext}"
        n += 1
    return name

def migrate(recycle_root: str, trash_dir: str) -> dict:
    photos_dir = os.path.join(recycle_root, "PHOTOS")
    legacy_thumbs = os.path.join(recycle_root, "_thumbs")
    trash_thumbs = os.path.join(trash_dir, "_thumbs")
    os.makedirs(trash_thumbs, exist_ok=True)
    # Council hardening: refuse cross-device (would force copy+delete on a near-full
    # disk, risking a half-written original). Same fs => rename is atomic.
    if os.stat(recycle_root).st_dev != os.stat(trash_dir).st_dev:
        raise RuntimeError("legacy bin and .ares-trash are on different filesystems; abort")

    originals = []
    for dirpath, _dirs, files in os.walk(photos_dir):
        for fn in files:
            full = os.path.join(dirpath, fn)
            sub = os.path.relpath(full, photos_dir)
            originals.append((full, sub))

    migrated = thumbs = missing = 0
    moved = []  # (trash_name, expected_size) for byte-level verify before delete
    for full, sub in originals:
        st = os.stat(full)
        original_path = reconstruct_original_path(sub)
        if _already_migrated(trash_dir, original_path):
            continue
        trash_name = unique_trash_name(trash_dir, st.st_mtime, os.path.basename(sub))
        try:
            shutil.move(full, os.path.join(trash_dir, trash_name))
        except OSError:
            continue
        meta = build_meta(original_path, trash_name, st.st_mtime, st.st_size)
        with open(os.path.join(trash_dir, trash_name + ".meta.json"), "w") as f:
            json.dump(meta, f, indent=2)
        moved.append((trash_name, st.st_size))
        migrated += 1
        hq, sd = legacy_thumb_basenames(sub)
        carried = False
        for cand in (hq, sd):
            src = os.path.join(legacy_thumbs, cand)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(trash_thumbs, trash_name + ".jpg"))
                carried = True
                break
        thumbs += 1 if carried else 0
        missing += 0 if carried else 1

    # Council hardening: byte-level verify gate. Every moved original must exist in
    # the trash at its recorded size before we delete the only remaining copy.
    bytes_ok = all(
        os.path.exists(os.path.join(trash_dir, tn))
        and os.path.getsize(os.path.join(trash_dir, tn)) == size
        for tn, size in moved
    )
    deleted = False
    remaining = [f for _d, _s, fs in os.walk(photos_dir) for f in fs]
    if migrated == len(originals) and bytes_ok and not remaining:
        shutil.rmtree(recycle_root)
        deleted = True
    return {"migrated": migrated, "thumbs": thumbs, "missing_thumbs": missing,
            "legacy_originals": len(originals), "deleted_legacy": deleted}

def _already_migrated(trash_dir: str, original_path: str) -> bool:
    import glob
    for mf in glob.glob(os.path.join(trash_dir, "*.meta.json")):
        try:
            if json.load(open(mf)).get("original_path") == original_path:
                return True
        except Exception:
            continue
    return False

def main():
    from system.recycling_bin import TRASH_DIR
    res = migrate("/mnt/nvme/PROMETHEUS/PHOTOS/RECYCLE_BIN", str(TRASH_DIR))
    print(res)

if __name__ == "__main__":
    main()
