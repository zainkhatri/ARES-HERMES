# Files Read-Write — Drag-Drop Upload, Move, Mkdir, Rename, Delete

**Status:** design, pending review
**Date:** 2026-09-16
**Author:** Zain

## Goal

Turn the read-only storage explorer at `/files` into a read-write file manager.
The user must be able to:

- Drag a folder from the Mac desktop into the browser and drop it anywhere; the
  folder tree is recreated on the server.
- Drag items inside the explorer onto another folder (grid tile or tree node) to
  move them.
- Create new folders.
- Rename items in place.
- Delete items to a recoverable trash.

The explorer today is `templates/files.html` (~1860 lines) backed by the
**read-only** `system/files_api.py`. That module's invariant — "never writes to
the browsed tree" — stays intact. All writes live in a new module.

## Non-goals

- No multi-user or concurrent-editor conflict model (single user).
- No versioning beyond the trash.
- No change to the gunicorn worker model.
- No writes inside protected islands (PHOTOS, MORDOR, the repo).

## Decisions (settled before design)

| Question | Decision |
|---|---|
| Operations | upload (incl. dropped folders), move, mkdir, rename, delete |
| Write scope | whole pool (`POOL_ROOT`), with guards |
| Delete model | move to `.ares-trash` (recoverable); reuse existing machinery |
| Conflict on upload/move | auto-rename (`name-2.ext`), surfaced to the user (not silent) |
| Protected islands | `PHOTOS/`, `MORDOR/`, `PROJECTS/ARES-DASHBOARD/` — browse-only |
| Upload transport | approach A — client walks the drop tree, one POST per file |

## Load-bearing runtime facts

These shaped the design and must not be assumed away:

1. **`POOL_ROOT` is ext4 on a single NVMe device** (`/dev/nvme0n1p1`). No
   copy-on-write snapshots → a `.trash` directory is the right undo mechanism,
   not filesystem snapshots. Same-device moves are atomic `rename(2)`; `EXDEV`
   cannot fire today but the fallback is kept for a future bind-mount.
2. **gunicorn runs `--workers 1 --threads 32`** (1 worker because in-process
   caches/locks must stay in one process; 32 threads serve concurrently). A big
   folder upload does **not** starve the dashboard. But every write helper is
   reachable by 32 threads at once and **must be thread-safe** — this is why the
   design relies on atomic syscalls (`O_EXCL`, `rename`) rather than
   check-then-act.
3. **An existing recycling bin already exists** — `system/recycling_bin.py`
   writes to `.ares-trash/<trash_name>` plus a `<trash_name>.meta.json` sidecar
   (`original_path`, `trash_name`, `trashed_at`, `size`) and exposes
   `list_trash()`, `restore()`, `purge_old(days=30)`. Reuse it. Note:
   `trash_file()` **refuses directories** on purpose (LLM-tool guard) — the
   Files feature needs a separate directory-capable entry point (below).

---

## Architecture

```
templates/files.html   (frontend — adds drag-drop, move, context menu, trash view)
        │  fetch() with X-ARES-Write: 1 header
        ▼
app.py  (thin route layer — 6 new endpoints, each @require_auth + CSRF header)
        │
        ▼
system/files_write.py  (NEW — all write logic, thread-safe, Power-of-Ten)
        │  reuses
        ├── files_api.safe_resolve()   (confinement + vault + dotfile gate)
        └── recycling_bin.*            (trash / restore / purge)
```

`files_api.py` is untouched except that `files_write` imports `safe_resolve`
from it. No write path exists that does not pass through `writable_target()`.

### The safety gate — `writable_target(rel) -> abspath | None`

Every write endpoint resolves **every** path it touches (source *and*
destination) through this one function.

```
writable_target(rel):
    assert isinstance(rel, str)
    ap = files_api.safe_resolve(rel)      # confinement + vault + dotfile (existing)
    if ap is None:
        return None                       # escaped root / vault / dotfile
    if _in_protected_island(ap):
        return None                       # PHOTOS / MORDOR / repo
    return ap
```

`_in_protected_island(ap)` compares the **realpath** of `ap` against the
realpaths of three island roots, blocking the root itself and anything under it:

- `POOL_ROOT/PHOTOS`
- `POOL_ROOT/MORDOR`
- the ARES-DASHBOARD repo dir (`Path(files_api.__file__).resolve().parents[1]`)

Realpath comparison means a symlink cannot dodge the island check.
`.ares-trash` is a dotfile directory, so `safe_resolve` already blocks the
explorer from writing into it directly.

Endpoints translate a `None` result into responses that **do not leak** why:

- escaped/absent path → `404` (identical to the read API)
- resolved-but-protected → `403 {"error": "protected area"}` (the frontend needs
  to tell the user "this folder is read-only", so the island case is
  distinguishable; a raw escape stays a 404)

To keep the 403/404 split without an oracle, endpoints first check
`safe_resolve` (→404 on None) and then `_in_protected_island` (→403). A path
that fails confinement never reaches the island check.

### The TOCTOU-safe write primitive — `safe_create(parent_ap, name)`

The council's key finding: `O_NOFOLLOW` on the leaf file is necessary but not
sufficient — a symlinked **parent** component can redirect a write outside
`POOL_ROOT` even after `safe_resolve` blessed the string. So creation walks and
opens each component itself.

```
safe_create(parent_ap, name) -> (fd, final_name):
    assert parent_ap and os.path.isabs(parent_ap)
    assert "/" not in name and name not in ("", ".", "..")
    dfd = os.open(parent_ap, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)  # parent, no symlink
    try:
        for i in range(0, MAX_SUFFIX):            # fixed bound (128), Power-of-Ten Rule 2
            cand = name if i == 0 else _bump(name, i)   # foo.txt -> foo-2.txt ...
            try:
                fd = os.open(cand, O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW, 0o644, dir_fd=dfd)
                return fd, cand
            except FileExistsError:
                continue
        raise OSError("too many name collisions")  # -> 409
    finally:
        os.close(dfd)
```

- `O_EXCL` makes create-or-clobber impossible → auto-rename is a bounded retry.
- `O_NOFOLLOW` on the leaf blocks a symlink-swap race.
- Operating relative to a `dir_fd` opened `O_DIRECTORY|O_NOFOLLOW` means a
  symlinked parent raises `ELOOP` instead of redirecting the write.

`safe_mkdirs(root_ap, rel_segments)` applies the same rule for upload folder
creation: walk the segments, `os.mkdir(seg, dir_fd=dfd)` catching `EEXIST`, then
re-open the child as the next `dir_fd` with `O_DIRECTORY|O_NOFOLLOW`. Fixed
bound `MAX_DEPTH = 32`. A symlinked intermediate raises `ELOOP` and the upload
of that file fails cleanly (400), the rest continue.

These two helpers are the reusable "one hardened door" the council recommended;
other routes that write may adopt them later (out of scope here).

### Upload file write — partial-safe

Each uploaded file is streamed to `finalname.part` (created via `safe_create`),
`fsync`'d, then atomically `rename`'d to its final name. A dropped connection
leaves a `.part` file, never a truncated file that looks complete. On the next
re-drop the `.part` is overwritten (its own `safe_create` bumps or the client
skip below avoids it). A sweep deletes stale `.part` files older than 1 day
(folded into the existing purge cron).

### Idempotent upload (free resumability + dedupe)

Before writing, if a regular file with the **same name, size, and mtime** already
exists in the destination, the server skips the write and returns
`{"ok": true, "skipped": true, "path": ...}`. Re-dropping a half-finished folder
then fills only the gaps. An optional `HEAD`-style precheck
(`GET /api/files/exists?dir=&name=&size=`) lets the client skip the upload
entirely for already-present files — no server-side upload state required.

---

## Backend — endpoints

All are `POST` (except `exists`), all `@require_auth`, all require the
`X-ARES-Write: 1` request header (CSRF, below), all return JSON.

| Endpoint | Body | Success | Notes |
|---|---|---|---|
| `/api/files/mkdir` | `{path, name}` | `{ok, path}` | `name` basename-only |
| `/api/files/rename` | `{path, new_name}` | `{ok, path, renamed_to}` | `new_name` basename-only; reject `/` |
| `/api/files/move` | `{src, dst_dir}` | `{ok, path, renamed_to?}` | both gated; auto-rename on clash |
| `/api/files/delete` | `{path}` | `{ok, trash_name}` | file or dir → `.ares-trash` |
| `/api/files/upload` | multipart: `dir`, `relpath`, `file` | `{ok, path, skipped?}` | one POST per file |
| `/api/files/exists` | query `dir,name,size` (GET) | `{exists, same}` | skip-precheck, read-only |

Trash browse/restore reuse the **existing** routes (`/api/trash/list`,
`/api/trash/restore`, purge cron) — the Files trash view is a frontend addition,
not new backend.

### Endpoint rules (Power-of-Ten)

Each handler, in order:

1. Assert method + required fields present and are `str` (≥2 assertions/fn).
2. `safe_resolve` each path → `404` on `None`.
3. `_in_protected_island` each path → `403 {"error":"protected area"}`.
4. For `rename`/`mkdir`: assert `new_name`/`name` is basename-only
   (`"/" not in name`, not `.`/`..`), else `400`.
5. Perform the op via `files_write`, checking every syscall return / errno.
6. Return the final path (post-auto-rename) so the UI shows the truth.

### Per-operation semantics

- **mkdir** — `safe_create`-style `os.mkdir(name, dir_fd=parent)`; `EEXIST` →
  auto-bump (`newfolder-2`); returns final name.
- **rename** — basename-only guard makes rename a same-directory op only (a name
  with `/` would be a disguised move that skips the dest gate → rejected). Uses
  `renameat(dir_fd, old, dir_fd, new)`; clash → auto-bump.
- **move** — `src` and `dst_dir` both gated. `os.rename(src, dst/finalname)`;
  clash inside `dst_dir` → `safe_create`-bumped name. On `EXDEV` (future
  cross-mount) → `shutil.move` (copies then unlinks source, preserving source on
  partial failure). Moving a directory is allowed (single subtree rename).
- **delete** — reuse the trash. Because `trash_file()` refuses directories, add
  `recycling_bin.trash_path(abspath)` that:
  - asserts `abspath` is absolute and (caller has already passed
    `writable_target`) re-asserts it is under `POOL_ROOT` and not a
    `_TRASH_DENY_PREFIXES` system path,
  - accepts files **and** directories,
  - builds `trash_name = <YYYYMMDD_HHMMSS>_<basename>` with a bounded uniqueness
    suffix (`_<n>`, ≤128) so two same-second deletes never collide,
  - `shutil.move`s into `.ares-trash`, writes the same `meta.json` shape
    (`_dir_size` already handles directories),
  - so `list_trash`/`restore`/`purge_old` work unchanged (they already branch on
    `is_dir`).
- **upload** — `dir` gated; `relpath` split into segments, each validated
  (`safe_mkdirs`, no `..`/absolute/`\x00`); file streamed `.part` → fsync →
  rename; idempotent skip as above.

### Bounds (Rule 2 — every loop fixed)

| Constant | Value | Meaning |
|---|---|---|
| `MAX_SUFFIX` | 128 | auto-rename retries before 409 |
| `MAX_DEPTH` | 32 | upload folder nesting |
| `MAX_UPLOAD_BYTES` | 2 GiB | per-file cap → 413 |
| `MAX_ENTRIES` (client) | 5000 | files per dropped folder |

---

## Frontend — `templates/files.html`

House style is fixed (see the existing tiles/brackets/segmented meters). New UI
must match; no independent design.

### CSRF header helper

A single `writeFetch(url, opts)` wrapper adds `X-ARES-Write: 1` to every
mutating request. All new calls go through it.

### Drop-to-upload

- A drop zone overlays the file grid; `dragenter/dragover` show a bracketed
  "Drop to upload here" state; `drop` reads `DataTransferItem` entries.
- **Folder walk is BFS with an explicit queue — no recursion** (Power-of-Ten
  Rule 1). Each `DirectoryReader.readEntries()` returns **≤100 children per
  call**, so each directory reader is drained in a loop until it returns empty —
  the classic bug is reading a folder once and silently uploading only its first
  100 files. Hard caps `MAX_ENTRIES=5000`, `MAX_DEPTH=32`; exceeding either
  stops the walk and tells the user what was dropped and what was skipped (no
  silent truncation).
- Files upload through a **client concurrency pool of 2–3** in-flight POSTs.
- **Progress is client-side** (it knows the total count) → a live
  "Uploading 340 / 2000 · myfolder/…" bar, no server polling.
- **Honest completion summary**: "1,980 uploaded, 20 skipped, 3 failed — Retry
  failed?". A stalled pool (no progress for N s, e.g. connection lost) shows
  "Upload paused — connection lost" rather than appearing to hang. Retry re-runs
  only the failed/remaining files (idempotent skip makes this safe).

### Drag-to-move

- Grid tiles and tree nodes become draggable and drop targets. Dragging a tile
  onto a folder tile, or onto a tree node, calls `/api/files/move`.
- The tree sidebar is a first-class drop target (same handler as grid).
- Dropping onto a **protected island** shows a no-drop cursor on hover and, if
  dropped, a clear toast: "PHOTOS is read-only — can't move here" (driven by the
  `403 protected area` response).
- Auto-rename is **surfaced**: if the server returns `renamed_to`, a toast says
  "Moved as budget-2.xlsx — a file with that name existed."

### New folder / rename / delete

- **New folder** — toolbar button + context-menu item; inline-editable name;
  calls `/api/files/mkdir`.
- **Rename** — context menu / F2; inline edit; `/api/files/rename`.
- **Delete** — context menu / Delete key. Confirm with the count for folders
  ("Move 214 items to Trash?"). After delete, a "Moved to Trash — Undo" toast;
  Undo calls the existing restore route with the returned `trash_name`.
- A right-click **context menu** hosts Open / Download / Rename / Move to… /
  Delete. It is new shared UI wired to the existing `selectedPath`/`dataset`
  model.

### Trash view (makes the invisible trash visible)

The Outsider's point: a hidden trash people can't open is just a scary word. Add
a **Trash** entry (sidebar or toolbar) that lists `/api/trash/list` items with
name, original path, age, and "purges in N days", plus Restore and Delete-forever
buttons. This reuses existing backend routes; it is the visible half of the
already-built recycling bin.

---

## Error handling

| Case | Response | UI |
|---|---|---|
| Path escapes root / vault / dotfile | 404 | generic "not found" |
| Path in protected island | 403 `protected area` | "X is read-only" toast, no-drop cursor |
| Name not basename-only | 400 | "invalid name" |
| Symlinked parent (ELOOP) | 400 | that one file fails; others continue |
| Collision > MAX_SUFFIX | 409 | "too many files with that name" |
| Upload > MAX_UPLOAD_BYTES | 413 | "file too large" |
| Missing CSRF header | 403 | (never happens from the SPA; blocks forged posts) |
| Disk full / OSError | 500 | that op fails; batch continues; summary counts it |

Partial batch uploads never report false success: the client tracks per-file
outcome and shows the honest summary.

## Security

- **Confinement** unchanged (`safe_resolve`), plus island gate on every write
  path (source and destination).
- **CSRF**: every mutating request requires `X-ARES-Write: 1`. A cross-origin
  page cannot set a custom header without a CORS preflight the server never
  approves; combined with `@require_auth` this blocks a malicious site in the
  logged-in browser from forging writes. Tailscale reachability is explicitly
  **not** treated as a CSRF boundary. (If the session cookie's `SameSite` is not
  already `Lax`/`Strict`, set it — belt-and-suspenders.)
- **Symlink/TOCTOU**: `O_NOFOLLOW`+`dir_fd` walk on create and mkdir; realpath
  island check; `O_EXCL` no-clobber.
- **Thread safety**: no check-then-act; atomic `O_EXCL`/`rename`; the trash
  `meta.json` write is per-item (unique name) so 32 threads don't collide.

## Testing

`tests/` — unit tests for `files_write` (no HTTP needed for the core):

1. `writable_target` returns None for: `../` escape, a vault path, a dotfile, and
   each of the three islands (root and a child); returns a real path for a normal
   dir.
2. `safe_create` — creates; auto-bumps on collision; refuses a `/` in name;
   raises on a symlinked parent (make a symlink dir in a tmp root); stops at
   `MAX_SUFFIX`.
3. `safe_mkdirs` — builds nested dirs; refuses `..` segment; refuses symlinked
   intermediate; stops at `MAX_DEPTH`.
4. `trash_path` — trashes a file and a directory; writes correct `meta.json`;
   `restore` round-trips both; refuses a system path.
5. move — same-dir rename; cross-dir move; auto-rename on clash; refuses move
   into an island (dest gate).
6. upload write — `.part` → rename leaves no `.part` on success; idempotent skip
   on identical name+size+mtime.

Endpoint smoke tests (with an auth session + `X-ARES-Write`): mkdir → upload one
file → move it → rename → delete → restore, asserting the tree state and the
403/404/400 boundaries. Missing `X-ARES-Write` → 403.

Manual: drag a real multi-hundred-file folder from the Mac desktop; confirm
progress, honest summary, and that a mid-upload tab close leaves no truncated
files. Drag onto PHOTOS → no-drop. Delete a folder → Trash view shows it →
Restore.

## Deploy notes / gotchas

- Writes land under `POOL_ROOT`; **uploading into the repo tree is blocked** by
  the repo island — this also prevents `watcher.py` from bouncing `ares`
  mid-upload on a stray `.py`.
- `.ares-trash` grows with folder deletes; the existing `purge_old(30)` cron
  covers it (now also sweeps stale `.part` files).
- No gunicorn change. No new dependency (stdlib `os`/`shutil` only).
- Restart after deploy: `pct exec 101 -- systemctl restart ares`.

## Out of scope (future)

- Copy (vs move), multi-select drag, cut/paste keyboard model.
- Adopting `safe_create`/`safe_mkdirs` across other write routes.
- A quota/size dashboard for `.ares-trash`.
