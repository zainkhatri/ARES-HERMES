# File Explorer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only, vault-excluded file explorer operation module to the ARES and ZEUS/HERMES dashboards.

**Architecture:** Pure, unit-testable logic (`safe_resolve`, `list_dir`, serve-policy, checked-open) lives in `system/files_api.py`; three thin `@require_auth` route handlers in `app.py` call into it and render/serve. Security funnels through one `safe_resolve()` chokepoint. Mirrored verbatim to the HERMES-DASH twin, which re-derives its own ROOT from `POOL_ROOT`.

**Tech Stack:** Python 3 / Flask (existing monolith), pytest, vanilla JS + HUD template CSS.

## Global Constraints

- **Read-only.** No route or helper may write, rename, move, delete, or create filesystem entries.
- **Vault never exposed.** `PHOTOS/.vault` and `vault_enc`/`vault_thumbs`/`vault_thumbs_hq`/`vault_video_cache`/`vault_hls`/`My Eyes Only` must never be listed or served. Dotfiles hidden by default.
- **Confinement on the resolved realpath**, not the request string. Fail closed.
- **ROOT** comes from `system.system_info.POOL_ROOT` (per-box correct). Assert it is a directory at import.
- **Identical `404`** for not-found and forbidden — never leak vault existence via status/timing.
- **Raw serving:** attachment by default; inline only for `image/*`, `video/*`, `audio/*`, `application/pdf`; text served as `text/plain`; always `X-Content-Type-Options: nosniff` + `Content-Security-Policy: sandbox`.
- **Power of Ten:** ≥2 assertions per function, validate every parameter, all loops bounded (listing cap 2000), max one level of pointer/deref-equivalent, zero warnings.
- **Commits:** subject line only — no body, no `Co-Authored-By` trailer (per repo rule).
- **No new dependencies.**

---

## Setup

- [ ] **Create a feature branch** (current branch is `sdd/terminal-xterm`, unrelated).

```bash
cd /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD
git checkout -b sdd/file-explorer
```

---

### Task 1: `safe_resolve` chokepoint + adversarial test

**Files:**
- Create: `system/files_api.py`
- Test: `tests/test_files_api.py`

**Interfaces:**
- Consumes: `system.system_info.POOL_ROOT` (str, already resolved per-box).
- Produces: `safe_resolve(rel: str, root: str = ROOT) -> str | None` — returns the confined absolute realpath, or `None` for any traversal/escape/vault/dotfile/invalid input. `ROOT: str` module constant. `DENY_NAMES: frozenset[str]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_files_api.py
import os, tempfile, shutil
from system import files_api

def _mk_root():
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, "PHOTOS", "2024"))
    os.makedirs(os.path.join(root, "PHOTOS", ".vault"))
    with open(os.path.join(root, "PHOTOS", "2024", "a.txt"), "w") as f:
        f.write("hi")
    with open(os.path.join(root, "PHOTOS", ".vault", "secret.txt"), "w") as f:
        f.write("nope")
    # legit in-root symlink (allowed), escape symlink (blocked), into-vault symlink (blocked)
    os.symlink(os.path.join(root, "PHOTOS", "2024"), os.path.join(root, "link_ok"))
    os.symlink("/etc", os.path.join(root, "link_escape"))
    os.symlink(os.path.join(root, "PHOTOS", ".vault"), os.path.join(root, "link_vault"))
    return root

def test_safe_resolve_rules():
    root = _mk_root()
    try:
        r = lambda rel: files_api.safe_resolve(rel, root=root)
        # accepts
        assert r("") == os.path.realpath(root)
        assert r("PHOTOS/2024") == os.path.join(os.path.realpath(root), "PHOTOS", "2024")
        assert r("link_ok") == os.path.join(os.path.realpath(root), "PHOTOS", "2024")  # in-root symlink ok
        # rejects
        assert r("../etc/passwd") is None
        assert r("/etc/passwd") is None
        assert r("PHOTOS/../../etc") is None
        assert r("link_escape") is None            # symlink out of root
        assert r("PHOTOS/.vault") is None          # dotfile + deny name
        assert r("link_vault") is None             # symlink resolving into vault
        assert r("PHOTOS/.vault/secret.txt") is None
        assert r("\x00") is None                    # NUL byte
    finally:
        shutil.rmtree(root)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_files_api.py -v`
Expected: FAIL — `AttributeError: module 'system.files_api' has no attribute 'safe_resolve'` (or ImportError).

- [ ] **Step 3: Write minimal implementation**

```python
# system/files_api.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_files_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add system/files_api.py tests/test_files_api.py
git commit -m "feat: add confined safe_resolve chokepoint for file explorer"
```

---

### Task 2: `kind_for` + `list_dir` (capped, filtered listing)

**Files:**
- Modify: `system/files_api.py`
- Test: `tests/test_files_api.py`

**Interfaces:**
- Consumes: `safe_resolve`, `ROOT`, `DENY_NAMES`.
- Produces:
  - `kind_for(name: str) -> str` — one of `pdf|image|video|audio|md|text|file`.
  - `list_dir(abspath: str, root: str = ROOT) -> dict` — `{"cwd": str, "parent": str|None, "entries": list, "truncated": bool}`; each entry `{"name","is_dir","size","mtime","kind"}`. `cwd`/`parent` are root-relative (`""` at root). Dirs first, then case-insensitive name sort. Dotfiles + `DENY_NAMES` excluded. Capped at `LIST_CAP` (2000).

- [ ] **Step 1: Write the failing test**

```python
def test_kind_for():
    from system import files_api
    assert files_api.kind_for("a.PDF") == "pdf"
    assert files_api.kind_for("a.jpg") == "image"
    assert files_api.kind_for("a.mp4") == "video"
    assert files_api.kind_for("a.flac") == "audio"
    assert files_api.kind_for("a.md") == "md"
    assert files_api.kind_for("a.py") == "text"
    assert files_api.kind_for("a.bin") == "file"

def test_list_dir_filters_and_sorts():
    import os, tempfile, shutil
    from system import files_api
    root = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(root, "zdir"))
        os.makedirs(os.path.join(root, "adir"))
        os.makedirs(os.path.join(root, ".vault"))       # excluded (deny + dot)
        for n in ("b.txt", "a.txt", ".hidden"):
            open(os.path.join(root, n), "w").close()
        out = files_api.list_dir(root, root=root)
        names = [e["name"] for e in out["entries"]]
        assert names == ["adir", "zdir", "a.txt", "b.txt"]   # dirs first, then name
        assert ".vault" not in names and ".hidden" not in names
        assert out["parent"] is None and out["cwd"] == ""
        assert out["truncated"] is False
        assert out["entries"][2]["kind"] == "text"
    finally:
        shutil.rmtree(root)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_files_api.py -k "kind_for or list_dir" -v`
Expected: FAIL — `kind_for`/`list_dir` not defined.

- [ ] **Step 3: Write minimal implementation**

```python
# append to system/files_api.py
LIST_CAP = 2000
_IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp", ".tiff", ".svg"}
_VIDEO = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
_AUDIO = {".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg"}
_TEXT = {".txt", ".log", ".csv", ".json", ".xml", ".yml", ".yaml", ".py",
         ".js", ".ts", ".html", ".css", ".sh", ".conf", ".ini", ".toml"}


def kind_for(name):
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
    """List one directory (no recursion). Excludes dotfiles + DENY_NAMES."""
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_files_api.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add system/files_api.py tests/test_files_api.py
git commit -m "feat: add capped, vault-filtered directory listing"
```

---

### Task 3: serve policy + checked open

**Files:**
- Modify: `system/files_api.py`
- Test: `tests/test_files_api.py`

**Interfaces:**
- Produces:
  - `serve_mode(name: str, force_dl: bool) -> tuple[str, bool]` — returns `(mimetype, as_attachment)`. Inline (`False`) only for pdf/image/video/audio and text (as `text/plain; charset=utf-8`); everything else (incl. html/svg) → `("application/octet-stream", True)`. `force_dl=True` always attachment.
  - `open_checked(abspath: str) -> int` — `os.open` with `O_RDONLY | O_NOFOLLOW`, `fstat` asserts a regular file; returns fd. Raises `OSError` on symlink-swap or non-regular file. Caller must close the fd.

- [ ] **Step 1: Write the failing test**

```python
def test_serve_mode_policy():
    from system import files_api
    assert files_api.serve_mode("a.pdf", False) == ("application/pdf", False)
    assert files_api.serve_mode("a.png", False)[1] is False
    assert files_api.serve_mode("a.py", False) == ("text/plain; charset=utf-8", False)
    assert files_api.serve_mode("evil.html", False) == ("application/octet-stream", True)
    assert files_api.serve_mode("evil.svg", False) == ("application/octet-stream", True)
    assert files_api.serve_mode("a.png", True)[1] is True     # force download

def test_open_checked_rejects_symlink():
    import os, tempfile, shutil, pytest
    from system import files_api
    d = tempfile.mkdtemp()
    try:
        real = os.path.join(d, "f.txt"); open(real, "w").close()
        link = os.path.join(d, "l.txt"); os.symlink(real, link)
        fd = files_api.open_checked(real); os.close(fd)        # regular file ok
        with pytest.raises(OSError):
            files_api.open_checked(link)                        # symlink rejected
    finally:
        shutil.rmtree(d)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_files_api.py -k "serve_mode or open_checked" -v`
Expected: FAIL — not defined.

- [ ] **Step 3: Write minimal implementation**

```python
# append to system/files_api.py
import mimetypes
import stat as _stat_mod

_INLINE_TOP = {"image", "video", "audio"}


def serve_mode(name, force_dl):
    """Decide (mimetype, as_attachment). Attachment-by-default; strict inline allowlist."""
    assert isinstance(name, str), "name must be str"
    assert isinstance(force_dl, bool), "force_dl must be bool"
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if force_dl:
        return mime, True
    top = mime.split("/", 1)[0]
    if mime == "application/pdf" or top in _INLINE_TOP:
        return mime, False
    if kind_for(name) in ("text", "md"):
        return "text/plain; charset=utf-8", False
    return "application/octet-stream", True


def open_checked(abspath):
    """Open a regular file without following a final symlink. Returns fd; caller closes."""
    assert isinstance(abspath, str) and abspath, "abspath required"
    fd = os.open(abspath, os.O_RDONLY | os.O_NOFOLLOW)
    st = os.fstat(fd)
    if not _stat_mod.S_ISREG(st.st_mode):
        os.close(fd)
        raise OSError("not a regular file")
    return fd
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_files_api.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add system/files_api.py tests/test_files_api.py
git commit -m "feat: add attachment-by-default serve policy and no-follow open"
```

---

### Task 4: Wire the three routes in `app.py`

**Files:**
- Modify: `app.py` (add import near line 22–26 alongside other `from system.*`; add routes near the VM/system routes, e.g. after line ~2935).

**Interfaces:**
- Consumes: `files_api.safe_resolve/list_dir/serve_mode/open_checked`, existing `require_auth`, Flask `request/jsonify/send_file/abort/Response`.
- Produces routes: `GET /files`, `GET /api/files/list`, `GET /api/files/raw`.

- [ ] **Step 1: Add the import**

Near the other `from system.*` imports (app.py ~line 26):
```python
from system import files_api
```

- [ ] **Step 2: Add the routes**

Insert after the VM routes block (after ~line 2935):
```python
@app.route("/files")
@require_auth
def files_view():
    return render_template("files.html", boot=get_system_info())


@app.route("/api/files/list")
@require_auth
def files_list():
    rel = request.args.get("path", "")
    ap = files_api.safe_resolve(rel)
    if not ap or not os.path.isdir(ap):
        abort(404)                                    # identical 404 for missing/forbidden
    return jsonify(files_api.list_dir(ap))


@app.route("/api/files/raw")
@require_auth
def files_raw():
    rel = request.args.get("path", "")
    force_dl = request.args.get("dl") == "1"
    ap = files_api.safe_resolve(rel)
    if not ap or not os.path.isfile(ap):
        abort(404)
    try:
        fd = files_api.open_checked(ap)               # reject symlink-swap / non-regular
        os.close(fd)
    except OSError:
        abort(404)
    mimetype, as_attachment = files_api.serve_mode(os.path.basename(ap), force_dl)
    resp = send_file(ap, mimetype=mimetype, as_attachment=as_attachment,
                     conditional=True, download_name=os.path.basename(ap))
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "sandbox"
    return resp
```

- [ ] **Step 3: Verify imports resolve** (the watcher auto-restarts; confirm no import error)

Run: `python -c "import app"` 2>&1 | tail -5`
Expected: no traceback (Flask app imports clean). If `send_file`/`abort` aren't already imported, add them to the existing `from flask import ...` line.

- [ ] **Step 4: Manual verify against real dirs** (service already running in LXC 101)

```bash
pct exec 101 -- systemctl restart ares && sleep 2
# list root (expects JSON with PHOTOS/PROJECTS/... and NO .vault)
curl -s -H "Authorization: Bearer $ARES_API_TOKEN" http://192.168.20.213:8080/api/files/list | python3 -m json.tool | head
# vault path must 404
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $ARES_API_TOKEN" "http://192.168.20.213:8080/api/files/list?path=PHOTOS/.vault"
# traversal must 404
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $ARES_API_TOKEN" "http://192.168.20.213:8080/api/files/raw?path=../../etc/passwd"
```
Expected: listing JSON without `.vault`; both edge cases return `404`.

- [ ] **Step 5: Commit**

```bash
git add app.py
git commit -m "feat: add /files and /api/files routes behind require_auth"
```

---

### Task 5: `files.html` page + home-nav op item

**Files:**
- Create: `templates/files.html`
- Modify: `templates/home.html` (ops nav ~line 596–617; count ~line 593)

**Interfaces:**
- Consumes: `/api/files/list?path=`, `/api/files/raw?path=&dl=`. Reuses `_hud_head.html`/`_hud_nav.html` includes and existing HUD CSS vars (`--ares`, `data-brand` palette).

- [ ] **Step 1: Add the op item to `home.html`**

After the Finance `.op` (line ~616), insert:
```html
<a href="/files" class="op">
    <span class="op-num">005</span>
    <span class="op-body"><span class="op-lab">Storage / Explorer</span><span class="op-name">Files</span></span>
    <svg class="op-arrow" width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M5 12h14m-6-6 6 6-6 6"/></svg>
</a>
```
And bump the module count (line ~593): `04 active` → `05 active`.

- [ ] **Step 2: Create `templates/files.html`**

Model the head/nav/theme on an existing simple page (e.g. `templates/drives.html`). Requirements the page must implement (vanilla JS, no new deps):
- Fetch `/api/files/list?path=<cwd>`; render breadcrumb from `cwd` (each segment clickable; a root/up affordance), then entries: type icon by `kind`, name, human size, date. Folders re-fetch `list`; files open the preview pane.
- **Preview pane:** `image`→`<img src=raw>`, `video`/`audio`→native player `src=raw`, `pdf`→`<iframe src=raw>`, `text`/`md`→`fetch(raw).then(text)` into `<pre>`. Every file shows a **Download** button → `raw?...&dl=1`. Unknown/`file` kind → metadata + Download only (no broken viewer).
- **Filter box labeled "Filter this folder"** — client-side, filters the current entries live.
- **Sort** control: name / date / size (client-side re-sort of the loaded array).
- If `truncated`, show "showing first 2000 — narrow with the filter".
- All `path` values URL-encoded in requests.

- [ ] **Step 3: Manual verify in browser**

Load `https://ares.tail3045df.ts.net/files`. Confirm: browse into PHOTOS/PROJECTS; `.vault` absent; open an image, a PDF, a text file (inline); download a `.zip`; filter narrows the list; breadcrumb navigates up.

- [ ] **Step 4: Commit**

```bash
git add templates/files.html templates/home.html
git commit -m "feat: add file explorer page and home nav module"
```

---

### Task 6: Mirror to HERMES-DASH (ZEUS)

**Files (on HERMES, via scp from ARES):**
- Create: `HERMES-DASH/system/files_api.py` (verbatim copy — ROOT comes from its own `POOL_ROOT`)
- Create: `HERMES-DASH/tests/test_files_api.py` (verbatim)
- Modify: `HERMES-DASH/app.py` (same import + 3 routes)
- Create: `HERMES-DASH/templates/files.html` (verbatim)
- Modify: `HERMES-DASH/templates/home.html` (same op item + count bump)

- [ ] **Step 1: Copy the module + template + test to HERMES**

```bash
BASE=/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD
DST=/srv/mergerfs/PROMETHEUS/HERMES-DASH
scp $BASE/system/files_api.py hermes:$DST/system/files_api.py
scp $BASE/tests/test_files_api.py hermes:$DST/tests/test_files_api.py
scp $BASE/templates/files.html hermes:$DST/templates/files.html
```

- [ ] **Step 2: Apply the same `app.py` + `home.html` edits on HERMES**

Apply Task 4 Step 1–2 and Task 5 Step 1 edits to `$DST/app.py` and `$DST/templates/home.html` (scp-edit or edit in place). Confirm HERMES's `system_info.POOL_ROOT` resolves to `/srv/mergerfs/PROMETHEUS`.

- [ ] **Step 3: Run the tests on HERMES + verify ROOT + non-root behavior**

```bash
ssh hermes 'cd /srv/mergerfs/PROMETHEUS/HERMES-DASH && python -m pytest tests/test_files_api.py -v && python -c "from system import files_api; print(files_api.ROOT)"'
```
Expected: tests PASS; `ROOT` prints `/srv/mergerfs/PROMETHEUS`.

- [ ] **Step 4: Restart HERMES dashboard + smoke test**

```bash
ssh hermes 'systemctl --user -M zain@ restart hermes-dash'
# then load https://hermes.tail3045df.ts.net/files — browse, confirm no vault, ZEUS (blue) theme
```
Expected: explorer works as non-root; blue theme; vault absent.

- [ ] **Step 5: Commit (both repos as applicable)**

```bash
cd /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD && git add -A && git commit -m "chore: note HERMES file explorer mirror"
# HERMES-DASH is a separate repo/codebase — commit there if it is version-controlled
```

---

## Self-Review

**Spec coverage:** every spec section maps to a task — `safe_resolve`/realpath confinement/vault/dotfile/env-ROOT-assert (T1), listing+cap+filter+sort (T2), attachment-default serve policy + O_NOFOLLOW/fstat (T3), routes + identical-404 + nosniff/CSP headers (T4), themed page + breadcrumbs/filter-label/sort/graceful-fallback + home nav (T5), HERMES mirror re-deriving ROOT (T6). The mandatory `_safe_resolve` adversarial test is T1.

**Placeholder scan:** no TBD/TODO; every code step shows real code; `files.html` is specified by concrete requirements (Task 5 Step 2) since it's a template, not unit-tested logic.

**Type consistency:** `safe_resolve`, `list_dir`, `kind_for`, `serve_mode`, `open_checked` signatures match between definition (T1–T3) and consumption (T4). ROOT/DENY_NAMES referenced consistently.

**Accepted ceilings:** single-worker stream stall (upgrade path = Caddy `X-Accel-Redirect`); no audit log / rate-limit in v1.
