"""Confined, island-guarded WRITE path for the file explorer.

This module is the ONLY sanctioned way to create/move/rename/delete inside the
browsed tree — the write-side counterpart to the read-only ``files_api``
(mirroring how ``photo_db`` is the only sanctioned DB writer). Do NOT hand-roll
an ``open()`` into the pool elsewhere; route it through here so confinement,
protected-island, and symlink-race defenses apply uniformly.

Safety model (see docs/superpowers/specs/2026-09-16-files-read-write-design.md):
  * every request path passes ``gate()`` = ``files_api.safe_resolve`` (confinement
    + vault + dotfile) plus a protected-island block (PHOTOS, MORDOR, the repo).
  * creation uses ``O_CREAT|O_EXCL|O_NOFOLLOW`` on the leaf and operates relative
    to a parent ``dir_fd`` opened ``O_DIRECTORY|O_NOFOLLOW`` — a symlinked parent
    raises instead of redirecting the write outside the pool.
  * name collisions auto-rename within a fixed bound; nothing is overwritten.
The 32-thread gunicorn worker means every helper must be thread-safe: it relies
on atomic syscalls, never check-then-act on a shared path.
"""
import errno
import os
import stat as _stat

from system import files_api

ROOT = files_api.ROOT

# Fixed upper bounds (Power-of-Ten Rule 2).
MAX_SUFFIX = 128                    # auto-rename attempts before giving up
MAX_DEPTH = 32                     # upload folder nesting
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024   # 2 GiB per file
_CHUNK = 1024 * 1024

# dir_fd relative syscalls are required; fail loudly at import if unsupported.
assert os.open in os.supports_dir_fd, "os.open needs dir_fd support"
assert os.mkdir in os.supports_dir_fd, "os.mkdir needs dir_fd support"
assert os.rename in os.supports_dir_fd, "os.rename needs dir_fd support"

# Protected islands: browse-only. Computed ROOT-relative so the /mnt/data
# bind-mount cannot shift them (do NOT derive from __file__).
_ISLANDS = tuple(os.path.realpath(os.path.join(ROOT, *parts)) for parts in (
    ("PHOTOS",),
    ("MORDOR",),
    ("PROJECTS", "ARES-DASHBOARD"),   # the running repo; a watcher restarts on .py writes
))


class ProtectedError(Exception):
    """Raised when a resolved path is inside a read-only island."""


def _in_protected_island(abspath):
    """True if abspath is a protected island or anything beneath one."""
    assert isinstance(abspath, str) and abspath, "abspath required"
    assert os.path.isabs(abspath), "abspath must be absolute"
    for island in _ISLANDS:
        if abspath == island or abspath.startswith(island + os.sep):
            return True
    return False


def gate(rel):
    """Resolve a user rel-path for WRITING. Returns (status, abspath).

    status is 'ok' | 'notfound' | 'protected'. 'notfound' covers a path that
    escapes root / hits the vault / is a dotfile (identical to the read API's
    404, no oracle). 'protected' is a resolved-but-island path (the UI needs to
    say "read-only"). Because _in_protected_island matches the island AND its
    subtree, gating each request path directly is sufficient — no parent walk.
    """
    assert isinstance(rel, str), "rel must be str"
    ap = files_api.safe_resolve(rel)
    if ap is None:
        return ("notfound", None)
    if _in_protected_island(ap):
        return ("protected", ap)
    return ("ok", ap)


def _valid_name(name):
    """A single path component that is safe to create."""
    assert isinstance(name, str), "name must be str"
    if name in ("", ".", "..") or "/" in name or "\x00" in name:
        return False
    if name.startswith("."):          # keep the tree dotfile-free, like the read side
        return False
    return len(name) <= 255


def _bump(name, i):
    """Insert -i before the extension: report.txt -> report-2.txt (i=1 -> -2)."""
    assert isinstance(name, str) and name, "name required"
    assert i >= 1, "bump index starts at 1"
    stem, ext = os.path.splitext(name)
    return "%s-%d%s" % (stem, i + 1, ext)


def _open_dir(name, parent_fd):
    """Open a child directory with no symlink following. Caller closes the fd."""
    assert isinstance(name, str) and name, "name required"
    assert isinstance(parent_fd, int) and parent_fd >= 0, "parent_fd invalid"
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                   dir_fd=parent_fd)


def make_dir(parent_ap, name):
    """Create a subdirectory under parent_ap; auto-rename on collision.

    Returns the final directory name. parent_ap MUST already be gate()'d 'ok'.
    """
    assert isinstance(parent_ap, str) and os.path.isabs(parent_ap), "parent_ap invalid"
    assert _valid_name(name), "invalid folder name"
    pfd = os.open(parent_ap, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for i in range(0, MAX_SUFFIX):
            cand = name if i == 0 else _bump(name, i)
            try:
                os.mkdir(cand, 0o755, dir_fd=pfd)
                return cand
            except FileExistsError:
                continue
        raise OSError(errno.EEXIST, "too many name collisions")
    finally:
        os.close(pfd)


def rename_item(target_ap, new_name):
    """Rename target_ap in place (same directory). Returns the final new name.

    new_name is a basename only — a '/' would make this a disguised move that
    skips the destination gate, so it is rejected upstream and here.
    """
    assert isinstance(target_ap, str) and os.path.isabs(target_ap), "target_ap invalid"
    assert _valid_name(new_name), "invalid new name"
    parent = os.path.dirname(target_ap)
    old = os.path.basename(target_ap)
    pfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        return _place(old, new_name, pfd, pfd)
    finally:
        os.close(pfd)


def move_into(src_ap, dst_dir_ap):
    """Move src_ap into directory dst_dir_ap; auto-rename on collision.

    Both paths MUST already be gate()'d 'ok'. Returns the final name in dst.
    """
    assert isinstance(src_ap, str) and os.path.isabs(src_ap), "src_ap invalid"
    assert isinstance(dst_dir_ap, str) and os.path.isdir(dst_dir_ap), "dst must be a dir"
    src_parent = os.path.dirname(src_ap)
    base = os.path.basename(src_ap)
    sfd = os.open(src_parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        dfd = os.open(dst_dir_ap, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            return _place(base, base, sfd, dfd)
        finally:
            os.close(dfd)
    finally:
        os.close(sfd)


def _place(old_name, want_name, src_fd, dst_fd):
    """Rename old_name (under src_fd) to a free name based on want_name (under
    dst_fd). Reserve the destination name atomically before renaming so two
    threads cannot pick the same target. Works for files and directories."""
    assert isinstance(old_name, str) and old_name, "old_name required"
    assert _valid_name(want_name), "invalid target name"
    st = os.stat(old_name, dir_fd=src_fd, follow_symlinks=False)
    src_is_dir = _stat.S_ISDIR(st.st_mode)
    for i in range(0, MAX_SUFFIX):
        cand = want_name if i == 0 else _bump(want_name, i)
        if cand == old_name and src_fd == dst_fd:
            return old_name              # rename to same name in same dir: no-op
        try:
            # Reserve the destination name atomically so two threads cannot pick
            # the same target; then rename our item onto the reservation. A
            # directory must be renamed onto an (empty) directory, a file onto a
            # file — reserve the matching kind.
            if src_is_dir:
                os.mkdir(cand, 0o755, dir_fd=dst_fd)
            else:
                fd = os.open(cand, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o644, dir_fd=dst_fd)
                os.close(fd)
        except FileExistsError:
            continue
        os.rename(old_name, cand, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
        return cand
    raise OSError(errno.EEXIST, "too many name collisions")


def write_upload(dir_ap, relpath, stream, max_bytes=MAX_UPLOAD_BYTES):
    """Stream one uploaded file to dir_ap/relpath, creating parent dirs.

    dir_ap MUST already be gate()'d 'ok'. relpath is the browser-supplied
    per-file relative path (e.g. "myfolder/sub/pic.jpg"); each segment is
    validated. Streams to a .part file (fsync'd) then atomically renames into
    place, so a dropped connection never leaves a truncated file looking whole.
    Auto-renames the leaf on collision. Returns the final leaf name.
    """
    assert isinstance(dir_ap, str) and os.path.isdir(dir_ap), "dir_ap must be a dir"
    assert isinstance(relpath, str) and relpath, "relpath required"
    segs = relpath.split("/")
    assert 1 <= len(segs) <= MAX_DEPTH, "relpath depth out of bounds"
    for seg in segs:                                    # bounded by MAX_DEPTH
        if not _valid_name(seg):
            raise ValueError("invalid path segment: %r" % seg)
    leaf = segs[-1]
    base_fd = os.open(dir_ap, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    dfd = base_fd
    opened = [base_fd]
    try:
        for seg in segs[:-1]:                            # bounded by MAX_DEPTH
            try:
                os.mkdir(seg, 0o755, dir_fd=dfd)
            except FileExistsError:
                pass
            child = _open_dir(seg, dfd)                  # O_NOFOLLOW: symlink -> ELOOP
            opened.append(child)
            dfd = child
        return _stream_to_leaf(leaf, stream, dfd, max_bytes)
    finally:
        for fd in opened:                                # bounded by MAX_DEPTH
            os.close(fd)


def _stream_to_leaf(leaf, stream, dfd, max_bytes):
    """Write stream to a .part file under dfd, fsync, atomically rename to a free
    name based on leaf. Enforces max_bytes while streaming (never trusts a
    header). Returns the final leaf name."""
    assert _valid_name(leaf), "invalid leaf name"
    assert max_bytes > 0, "max_bytes must be positive"
    part_name = leaf + ".part"
    for i in range(0, MAX_SUFFIX):                       # find a free .part name
        cand_part = part_name if i == 0 else _bump(part_name, i)
        try:
            fd = os.open(cand_part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o644, dir_fd=dfd)
            break
        except FileExistsError:
            continue
    else:
        raise OSError(errno.EEXIST, "too many .part collisions")
    total = 0
    try:
        while True:                                      # bounded by max_bytes below
            chunk = stream.read(_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("upload exceeds %d bytes" % max_bytes)
            os.write(fd, chunk)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(cand_part, dir_fd=dfd)
        except OSError:
            pass
        raise
    os.close(fd)
    # Reserve the final name and rename our .part onto it (atomic, no clobber).
    for i in range(0, MAX_SUFFIX):
        cand = leaf if i == 0 else _bump(leaf, i)
        try:
            rfd = os.open(cand, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                          0o644, dir_fd=dfd)
            os.close(rfd)
        except FileExistsError:
            continue
        os.rename(cand_part, cand, src_dir_fd=dfd, dst_dir_fd=dfd)
        return cand
    try:
        os.unlink(cand_part, dir_fd=dfd)
    except OSError:
        pass
    raise OSError(errno.EEXIST, "too many name collisions")
