"""Recycling bin for ARES. Moves files to trash instead of deleting them."""

import json
import os
import secrets
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
# On NAS: /srv/mergerfs/PROMETHEUS/ARES → trash lives at /srv/mergerfs/PROMETHEUS/.ares-trash
# On Mac: /Volumes/PROMETHEUS/ARES → trash lives at /Volumes/PROMETHEUS/.ares-trash
TRASH_DIR = _PROJECT_ROOT.parent / ".ares-trash"
TRASH_DIR.mkdir(parents=True, exist_ok=True)


def _meta_path(trash_name: str) -> Path:
    return TRASH_DIR / f"{trash_name}.meta.json"


_TRASH_DENY_PREFIXES = ("/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot",
                        "/var", "/root", "/proc", "/sys", "/dev", "/run")

def trash_file(file_path: str) -> dict:
    """Move a FILE to the recycling bin with metadata."""
    src = Path(file_path).resolve()
    if not src.exists():
        return {"success": False, "error": f"Path does not exist: {file_path}"}
    # This path can come from the LLM tool (`/api/chat` → trash_file) with model-supplied input.
    # Refuse to move a whole DIRECTORY (one call could relocate the entire photo library) and refuse
    # system paths — the legit callers (trash a photo) always pass an individual file under the pool.
    if src.is_dir():
        return {"success": False, "error": "refusing to trash a directory"}
    sp = str(src)
    if sp == "/" or any(sp == p or sp.startswith(p + "/") for p in _TRASH_DENY_PREFIXES):
        return {"success": False, "error": "refusing to trash a system path"}

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    trash_name = f"{timestamp}_{src.name}"
    dest = TRASH_DIR / trash_name

    try:
        shutil.move(str(src), str(dest))
    except Exception as e:
        return {"success": False, "error": f"Failed to move to trash: {e}"}

    meta = {
        "original_path": str(src),
        "trash_name": trash_name,
        "trashed_at": datetime.now().isoformat(),
        "size": dest.stat().st_size if dest.is_file() else _dir_size(dest),
    }
    _meta_path(trash_name).write_text(json.dumps(meta, indent=2))

    return {"success": True, "message": f"Moved to trash: {src.name}", "trash_name": trash_name}


def trash_path(abspath: str) -> dict:
    """Move a FILE or DIRECTORY to the recycling bin. Files-explorer entry point.

    Unlike trash_file(), this accepts directories — the caller (the file-explorer
    delete route) has already vetted `abspath` through files_write.gate() so it is
    confined and not inside a protected island. We still refuse system paths as
    defense-in-depth. The trash_name carries a random token so two same-second,
    same-name deletes across the 32 threads cannot collide (a bare timestamp name
    would let shutil.move merge/overwrite a prior trashed directory). Directory
    size is NOT walked (rglob would pin a thread on a huge tree) — stored as None.
    """
    assert isinstance(abspath, str) and abspath, "abspath required"
    assert os.path.isabs(abspath), "abspath must be absolute"
    src = Path(abspath).resolve()
    if not src.exists():
        return {"success": False, "error": f"Path does not exist: {abspath}"}
    sp = str(src)
    if sp == "/" or any(sp == p or sp.startswith(p + "/") for p in _TRASH_DENY_PREFIXES):
        return {"success": False, "error": "refusing to trash a system path"}

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    is_dir = src.is_dir()
    for _attempt in range(0, 16):                       # bounded uniqueness retry
        token = secrets.token_hex(4)
        trash_name = f"{timestamp}_{token}_{src.name}"
        dest = TRASH_DIR / trash_name
        if not dest.exists() and not _meta_path(trash_name).exists():
            break
    else:
        return {"success": False, "error": "could not allocate a unique trash name"}

    try:
        shutil.move(str(src), str(dest))               # handles cross-device (EXDEV) itself
    except Exception as e:
        return {"success": False, "error": f"Failed to move to trash: {e}"}

    meta = {
        "original_path": str(src),
        "trash_name": trash_name,
        "trashed_at": datetime.now().isoformat(),
        "is_dir": is_dir,
        "size": None if is_dir else (dest.stat().st_size if dest.is_file() else None),
    }
    _meta_path(trash_name).write_text(json.dumps(meta, indent=2))
    return {"success": True, "message": f"Moved to trash: {src.name}",
            "trash_name": trash_name}


def restore_gated(trash_name: str, gate):
    """Restore a trashed item, but re-validate its destination through `gate`.

    `gate(abspath) -> bool` must return True only if abspath is a safe, confined,
    non-island place to write (the file-explorer passes files_write's check). The
    legacy restore() trusts the stored original_path blindly, which would let an
    Undo drop a file into PHOTOS/MORDOR/the repo — this variant refuses that, and
    auto-renames instead of failing when the original location is now occupied.
    Returns {success, message|error, restored_to?}.
    """
    assert isinstance(trash_name, str) and trash_name, "trash_name required"
    assert callable(gate), "gate must be callable"
    item_path = TRASH_DIR / trash_name
    try:
        item_path.resolve().relative_to(Path(TRASH_DIR).resolve())
    except ValueError:
        return {"success": False, "error": "invalid name"}
    meta_file = _meta_path(trash_name)
    if not item_path.exists():
        return {"success": False, "error": f"Item not found in trash: {trash_name}"}
    if not meta_file.exists():
        return {"success": False, "error": f"Metadata not found for: {trash_name}"}

    meta = json.loads(meta_file.read_text())
    original = Path(meta["original_path"])
    parent = original.parent
    if not gate(str(parent)):
        return {"success": False,
                "error": "cannot restore here — destination is read-only or outside the files area"}

    parent.mkdir(parents=True, exist_ok=True)
    dest = original
    if dest.exists():                                   # occupied -> bump, never clobber
        stem, ext = os.path.splitext(original.name)
        for i in range(2, 2 + 128):                     # bounded
            cand = parent / f"{stem}-{i}{ext}"
            if not cand.exists():
                dest = cand
                break
        else:
            return {"success": False, "error": "too many name collisions at destination"}

    try:
        shutil.move(str(item_path), str(dest))
        meta_file.unlink()
        return {"success": True, "message": f"Restored to: {dest}", "restored_to": str(dest)}
    except Exception as e:
        return {"success": False, "error": f"Restore failed: {e}"}


def list_trash() -> list[dict]:
    """List all items in the recycling bin."""
    items = []
    for meta_file in sorted(TRASH_DIR.glob("*.meta.json")):
        try:
            meta = json.loads(meta_file.read_text())
            age = datetime.now() - datetime.fromisoformat(meta["trashed_at"])
            meta["age_days"] = age.days
            meta["age_str"] = f"{age.days}d {age.seconds // 3600}h ago"
            meta["purge_in"] = max(0, 30 - age.days)
            items.append(meta)
        except Exception:
            continue
    return items


def restore(trash_name: str) -> dict:
    """Restore an item from the recycling bin to its original location."""
    item_path = TRASH_DIR / trash_name
    # trash_name is client-supplied — it must resolve inside TRASH_DIR (no ../ escape).
    try:
        item_path.resolve().relative_to(Path(TRASH_DIR).resolve())
    except ValueError:
        return {"success": False, "error": "invalid name"}
    meta_file = _meta_path(trash_name)

    if not item_path.exists():
        return {"success": False, "error": f"Item not found in trash: {trash_name}"}

    if not meta_file.exists():
        return {"success": False, "error": f"Metadata not found for: {trash_name}"}

    meta = json.loads(meta_file.read_text())
    original = Path(meta["original_path"])

    if original.exists():
        return {"success": False, "error": f"Original path already exists: {original}"}

    original.parent.mkdir(parents=True, exist_ok=True)

    try:
        shutil.move(str(item_path), str(original))
        meta_file.unlink()
        return {"success": True, "message": f"Restored to: {original}"}
    except Exception as e:
        return {"success": False, "error": f"Restore failed: {e}"}


def purge_old(days: int = 30) -> dict:
    """Permanently delete items older than `days` days."""
    purged = []
    for meta_file in TRASH_DIR.glob("*.meta.json"):
        try:
            meta = json.loads(meta_file.read_text())
            trashed_at = datetime.fromisoformat(meta["trashed_at"])
            if datetime.now() - trashed_at > timedelta(days=days):
                trash_name = meta["trash_name"]
                item_path = TRASH_DIR / trash_name
                if item_path.is_dir():
                    shutil.rmtree(item_path)
                elif item_path.exists():
                    item_path.unlink()
                meta_file.unlink()
                purged.append(trash_name)
        except Exception:
            continue
    return {"purged_count": len(purged), "purged": purged}


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "purge":
        result = purge_old()
        print(f"Purged {result['purged_count']} items older than 30 days.")
