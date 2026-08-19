"""Read-only file explorer: confined, vault-excluded filesystem access.

All filesystem paths MUST pass through safe_resolve() before use. Read-only:
this module never writes, renames, or deletes.
"""
import os
from system.system_info import POOL_ROOT

ROOT = os.path.realpath(POOL_ROOT)
assert os.path.isdir(ROOT), f"files_api: POOL_ROOT not a directory: {ROOT!r}"

# Defense-in-depth denylist (dotfiles are also hidden, which covers .vault).
DENY_NAMES = frozenset({
    ".vault", "vault_enc", "vault_thumbs", "vault_thumbs_hq",
    "vault_video_cache", "vault_hls", "My Eyes Only",
})


def safe_resolve(rel, root=ROOT):
    """Return the confined absolute realpath for user-supplied rel, else None.

    Confinement + vault/dotfile checks run on the RESOLVED realpath so a
    symlink pointing outside root or into the vault fails closed.
    """
    assert isinstance(rel, str), "rel must be str"
    assert isinstance(root, str) and root, "root must be non-empty str"
    if "\x00" in rel or os.path.isabs(rel):
        return None
    root = os.path.realpath(root)
    abspath = os.path.realpath(os.path.join(root, rel))
    if abspath != root and not abspath.startswith(root + os.sep):
        return None
    inside = abspath[len(root):]                      # "" at root, else "/a/b"
    for seg in inside.split(os.sep):
        if not seg:
            continue
        if seg in DENY_NAMES or seg.startswith("."):
            return None
    return abspath


LIST_CAP = 2000
_IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp", ".tiff", ".svg"}
_VIDEO = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
_AUDIO = {".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg"}
_TEXT = {".txt", ".log", ".csv", ".json", ".xml", ".yml", ".yaml", ".py",
         ".js", ".ts", ".html", ".css", ".sh", ".conf", ".ini", ".toml"}


def kind_for(name):
    """Classify file by extension. Returns one of: pdf|image|video|audio|md|text|file."""
    assert isinstance(name, str), "name must be str"
    ext = os.path.splitext(name)[1].lower()
    assert isinstance(ext, str)
    if ext == ".pdf":
        return "pdf"
    if ext in _IMAGE:
        return "image"
    if ext in _VIDEO:
        return "video"
    if ext in _AUDIO:
        return "audio"
    if ext == ".md":
        return "md"
    if ext in _TEXT:
        return "text"
    return "file"


def list_dir(abspath, root=ROOT):
    """List one directory (no recursion). Excludes dotfiles + DENY_NAMES.

    Returns {"cwd": str, "parent": str|None, "entries": list, "truncated": bool}.
    cwd/parent are root-relative ("" at root). Dirs first, then case-insensitive sort.
    Capped at LIST_CAP (2000) with truncated flag.
    """
    assert isinstance(abspath, str) and abspath, "abspath required"
    assert os.path.isdir(abspath), "abspath must be a directory"
    root = os.path.realpath(root)
    entries = []
    truncated = False
    with os.scandir(abspath) as it:
        for de in it:                                 # bounded by LIST_CAP below
            if de.name.startswith(".") or de.name in DENY_NAMES:
                continue
            if len(entries) >= LIST_CAP:
                truncated = True
                break
            try:
                st = de.stat(follow_symlinks=False)
                is_dir = de.is_dir()
            except OSError:
                continue
            entries.append({
                "name": de.name,
                "is_dir": is_dir,
                "size": None if is_dir else st.st_size,
                "mtime": int(st.st_mtime),
                "kind": "dir" if is_dir else kind_for(de.name),
            })
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    rel = "" if os.path.realpath(abspath) == root else os.path.relpath(abspath, root)
    parent = None if rel == "" else os.path.dirname(rel)
    return {"cwd": rel, "parent": parent, "entries": entries, "truncated": truncated}
