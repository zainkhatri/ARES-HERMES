"""Photo/video-only view over the .ares-trash recycling bin."""
import os, json
from datetime import datetime

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".heic", ".webp",
              ".mp4", ".mov", ".m4v", ".avi", ".mkv"}

def _is_media(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in PHOTO_EXTS

def list_photo_trash(trash_dir: str) -> list[dict]:
    thumbs = os.path.join(trash_dir, "_thumbs")
    items = []
    for fn in os.listdir(trash_dir):
        if not fn.endswith(".meta.json"):
            continue
        try:
            meta = json.load(open(os.path.join(trash_dir, fn)))
        except Exception:
            continue
        op = meta.get("original_path", "")
        if not _is_media(op):
            continue
        try:
            age = datetime.now() - datetime.fromisoformat(meta["trashed_at"])
            age_str = f"{age.days}d ago"; purge_in = max(0, 30 - age.days)
        except Exception:
            age_str = "?"; purge_in = 30
        tn = meta["trash_name"]
        items.append({
            "trash_name": tn,
            "original_path": op,
            "filename": os.path.basename(op),
            "trashed_at": meta.get("trashed_at", ""),
            "age_str": age_str,
            "purge_in": purge_in,
            "has_thumb": os.path.exists(os.path.join(thumbs, tn + ".jpg")),
        })
    items.sort(key=lambda i: i["trashed_at"], reverse=True)
    return items

def trash_thumb_path(trash_dir: str, trash_name: str) -> str:
    return os.path.join(trash_dir, "_thumbs", trash_name + ".jpg")

def original_file_path(trash_dir: str, trash_name: str) -> str:
    return os.path.join(trash_dir, trash_name)

def _within_trash(trash_dir: str, name: str) -> bool:
    """trash_name comes from the client — it must resolve INSIDE trash_dir (no ../ escape),
    or a purge/restore could os.remove / move an arbitrary file on the host."""
    root = os.path.realpath(trash_dir)
    p = os.path.realpath(os.path.join(trash_dir, name))
    return p == root or p.startswith(root + os.sep)

def purge_item(trash_dir: str, trash_name: str) -> dict:
    if not _within_trash(trash_dir, trash_name):
        return {"success": False, "error": "invalid name"}
    item = os.path.join(trash_dir, trash_name)
    if not os.path.exists(item):
        return {"success": False, "error": f"Not in trash: {trash_name}"}
    for p in (item, item + ".meta.json", trash_thumb_path(trash_dir, trash_name)):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError as e:
            return {"success": False, "error": str(e)}
    return {"success": True, "message": f"Purged {trash_name}"}

def empty_bin(trash_dir: str) -> dict:
    purged = 0
    for it in list_photo_trash(trash_dir):
        if purge_item(trash_dir, it["trash_name"]).get("success"):
            purged += 1
    return {"purged": purged}
