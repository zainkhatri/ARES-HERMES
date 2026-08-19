"""Read-only file explorer: confined, vault-excluded filesystem access.

All filesystem paths MUST pass through safe_resolve() before use. Read-only:
this module never writes to the browsed tree (the only writes are cached
thumbnails, in a private cache dir outside the tree).
"""
import os
import mimetypes
import stat as _stat_mod
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
    assert ext == "" or ext.startswith("."), "ext must be empty or dotted"
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
    try:
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
    except OSError:
        pass  # ponytail: unreadable dir (e.g. lost+found) → return empty entries
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    rel = "" if os.path.realpath(abspath) == root else os.path.relpath(abspath, root)
    parent = None if rel == "" else os.path.dirname(rel)
    return {"cwd": rel, "parent": parent, "entries": entries, "truncated": truncated}


_INLINE_TOP = {"image", "video", "audio"}
_TEXT_SAFE = {".txt", ".log", ".csv", ".json", ".xml", ".yml", ".yaml", ".py",
             ".js", ".ts", ".css", ".sh", ".conf", ".ini", ".toml"}


def serve_mode(name, force_dl):
    """Decide (mimetype, as_attachment). Attachment-by-default; strict inline allowlist."""
    assert isinstance(name, str), "name must be str"
    assert isinstance(force_dl, bool), "force_dl must be bool"
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if force_dl:
        return mime, True
    ext = os.path.splitext(name)[1].lower()
    if ext == ".svg":
        return "application/octet-stream", True
    top = mime.split("/", 1)[0]
    if mime == "application/pdf" or top in _INLINE_TOP:
        return mime, False
    if ext in _TEXT_SAFE or kind_for(name) == "md":
        return "text/plain", False
    return "application/octet-stream", True


def open_checked(abspath):
    """Open a regular file without following a final symlink. Returns fd; caller closes."""
    assert isinstance(abspath, str) and abspath, "abspath required"
    assert os.path.isabs(abspath), "abspath must be absolute"
    fd = os.open(abspath, os.O_RDONLY | os.O_NOFOLLOW)
    st = os.fstat(fd)
    if not _stat_mod.S_ISREG(st.st_mode):
        os.close(fd)
        raise OSError("not a regular file")
    return fd


# ── Thumbnails (Finder-style grid previews) ──────────────────────────────────
# Small preview JPEGs for image/video/pdf. Writes ONLY to a private cache dir
# (never the browsed tree). Bounded concurrency + lazy client requests keep a
# large folder from wedging the box. PIL/pymupdf imported lazily so a missing
# dep degrades to "no thumbnail" (404) instead of breaking module import.
import hashlib as _hashlib
import subprocess as _sp
import tempfile as _tempfile
import threading as _threading

THUMB_PX = 320
THUMB_KINDS = frozenset({"image", "video", "pdf"})
THUMB_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_files_thumb_cache")
try:
    os.makedirs(THUMB_CACHE, exist_ok=True)
except OSError:
    THUMB_CACHE = os.path.join(_tempfile.gettempdir(), "files_thumb_cache")
    os.makedirs(THUMB_CACHE, exist_ok=True)
_thumb_sema = _threading.Semaphore(4)


def _thumb_cache_path(abspath, st):
    key = _hashlib.sha1(
        ("%s|%d|%d|%d" % (abspath, int(st.st_mtime), st.st_size, THUMB_PX)).encode()
    ).hexdigest()
    return os.path.join(THUMB_CACHE, key + ".jpg")


def _thumb_image(src, dst):
    from PIL import Image, ImageOps
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        im.thumbnail((THUMB_PX, THUMB_PX))
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.save(dst, "JPEG", quality=82)


def _thumb_video(src, dst):
    # Stage the ffmpeg frame IN the cache dir (same filesystem as dst) so the
    # os.replace is a same-fs atomic rename — a /tmp intermediate would be a
    # cross-device link and fail. ffmpeg needs a real image extension.
    frame = dst + ".frame.jpg"
    try:
        _sp.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "1", "-i", src,
                 "-vframes", "1", "-vf", "scale=%d:-2" % THUMB_PX, frame],
                timeout=20, check=True, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        os.replace(frame, dst)
    finally:
        if os.path.exists(frame):
            os.remove(frame)


def _thumb_pdf(src, dst):
    import pymupdf
    doc = pymupdf.open(src)
    try:
        page = doc.load_page(0)
        r = page.rect
        zoom = THUMB_PX / max(r.width, r.height, 1)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        with open(dst, "wb") as f:
            f.write(pix.tobytes("jpg"))
    finally:
        doc.close()


def make_thumb(abspath):
    """Return a cached JPEG thumbnail path for abspath, or None if not
    thumbnailable / generation failed. abspath MUST already be vetted by
    safe_resolve. Cache keyed by path+mtime+size; generation is bounded."""
    assert isinstance(abspath, str) and abspath, "abspath required"
    assert os.path.isabs(abspath), "abspath must be absolute"
    kind = kind_for(os.path.basename(abspath))
    if kind not in THUMB_KINDS:
        return None
    try:
        st = os.stat(abspath)
    except OSError:
        return None
    cache = _thumb_cache_path(abspath, st)
    if os.path.exists(cache):
        return cache
    with _thumb_sema:
        if os.path.exists(cache):
            return cache
        tmp = cache + ".tmp"
        try:
            if kind == "image":
                _thumb_image(abspath, tmp)
            elif kind == "video":
                _thumb_video(abspath, tmp)
            else:
                _thumb_pdf(abspath, tmp)
            os.replace(tmp, cache)
            return cache
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return None
