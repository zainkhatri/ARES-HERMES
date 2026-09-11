# Photos Recycle Bin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the orphaned legacy `PHOTOS/RECYCLE_BIN` into the current `.ares-trash` bin, then add a Photos "Recycle Bin" page to review, restore, or permanently delete trashed photos.

**Architecture:** Testable logic lives in two small modules — `scripts/migrate_recycle_bin.py` (one-time migration) and `photos/trash_review.py` (list/purge/thumb helpers) — unit-tested with tempdir fixtures like `tests/test_files_api.py`. `app.py` gains thin route wrappers. The review UI is a standalone `templates/recycle_bin.html` page mirroring the existing `dupes_review.html` card grid, reached by one link in the Photos view.

**Tech Stack:** Python 3, Flask, pytest, vanilla JS templates. Existing helpers: `system/recycling_bin.py` (`TRASH_DIR`, `list_trash`, `restore`).

## Global Constraints

- Trash bin path: `TRASH_DIR = _PROJECT_ROOT.parent / ".ares-trash"` — import from `system/recycling_bin.py`, never hardcode.
- Legacy bin: `/mnt/nvme/PROMETHEUS/PHOTOS/RECYCLE_BIN` (host) — originals under `PHOTOS/<subpath>`, thumbs under `_thumbs/`.
- Original-path format in meta must be container view: `/mnt/data/PROMETHEUS/PHOTOS/<subpath>` (matches existing `.ares-trash` meta and what `restore()` expects).
- Legacy thumb naming: `thumbs_<md5(subpath)>.jpg` and `thumbs_hq_<md5(subpath)>.jpg`, `subpath` relative to `PHOTOS/`.
- Meta.json shape (unchanged from current bin): `{original_path, trash_name, trashed_at (ISO), size}`.
- All new routes use `@require_auth`.
- Delete the legacy directory ONLY after every original is verified migrated.
- Git: repo is not initialized here; "commit" steps are no-ops — instead, after each task run the tests and confirm green before moving on.

---

## Task 0: Pre-flight verification — COMPLETE (2026-09-04)

Ran before writing code, per LLM Council review. All checks passed on real data:

- **Path format:** all 47 live-written `.ares-trash` metas use `/mnt/data/PROMETHEUS/PHOTOS/...`
  — matches the plan's reconstruction exactly. (CLAUDE.md's `/mnt/data/PHOTOS` is stale.)
- **Restore root:** `/mnt/data/PROMETHEUS/PHOTOS` exists inside LXC 101 → restore lands correctly.
- **Legacy count:** exactly **2114** originals under `RECYCLE_BIN/PHOTOS`.
- **Same filesystem:** legacy and `.ares-trash` share dev `66310` → `os.rename` is atomic, no cross-device copy.
- **Thumb md5 map:** **2114/2114 (100%)** originals match `thumbs_<md5(subpath)>.jpg` — mapping is exact, not a coincidence.
- **No double-counting:** 0 inode overlap between legacy files and live trash — genuinely separate files.

**Still required before the real run (Task 8):** `cp -a` backup of the legacy dir, and one live trash→restore round-trip of a throwaway photo.

---

### Task 1: Migration pure helpers

**Files:**
- Create: `scripts/migrate_recycle_bin.py`
- Test: `tests/test_migrate_recycle_bin.py`

**Interfaces:**
- Produces:
  - `reconstruct_original_path(subpath: str, photo_root: str = "/mnt/data/PROMETHEUS/PHOTOS") -> str`
  - `legacy_thumb_basenames(subpath: str) -> tuple[str, str]` → `(hq_name, sd_name)`
  - `build_meta(original_path: str, trash_name: str, mtime: float, size: int) -> dict`
  - `unique_trash_name(dest_dir: str, mtime: float, basename: str) -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migrate_recycle_bin.py
import os, tempfile, hashlib, json
from scripts import migrate_recycle_bin as m

def test_reconstruct_original_path():
    assert m.reconstruct_original_path("S95/2025/IMG_34267.jpg") == \
        "/mnt/data/PROMETHEUS/PHOTOS/S95/2025/IMG_34267.jpg"

def test_legacy_thumb_basenames():
    sub = "S95/2025/IMG_34267.jpg"
    h = hashlib.md5(sub.encode()).hexdigest()
    hq, sd = m.legacy_thumb_basenames(sub)
    assert hq == f"thumbs_hq_{h}.jpg"
    assert sd == f"thumbs_{h}.jpg"

def test_build_meta_shape():
    meta = m.build_meta("/mnt/data/PROMETHEUS/PHOTOS/a/b.jpg", "20260101_000000_b.jpg", 1700000000, 123)
    assert meta["original_path"] == "/mnt/data/PROMETHEUS/PHOTOS/a/b.jpg"
    assert meta["trash_name"] == "20260101_000000_b.jpg"
    assert meta["size"] == 123
    assert meta["trashed_at"].startswith("20")  # ISO string

def test_unique_trash_name_collision():
    d = tempfile.mkdtemp()
    n1 = m.unique_trash_name(d, 1700000000, "IMG.jpg")
    open(os.path.join(d, n1), "w").close()
    n2 = m.unique_trash_name(d, 1700000000, "IMG.jpg")
    assert n1 != n2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD && python -m pytest tests/test_migrate_recycle_bin.py -v`
Expected: FAIL with `ModuleNotFoundError` or `AttributeError` (functions not defined).

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/migrate_recycle_bin.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_migrate_recycle_bin.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Checkpoint** — confirm 4 tests green before Task 2.

---

### Task 2: Migration runner with verify-before-delete gate

**Files:**
- Modify: `scripts/migrate_recycle_bin.py` (add `migrate()` + `main()`)
- Test: `tests/test_migrate_recycle_bin.py` (add runner tests)

**Interfaces:**
- Consumes: `reconstruct_original_path`, `legacy_thumb_basenames`, `build_meta`, `unique_trash_name` (Task 1).
- Produces: `migrate(recycle_root: str, trash_dir: str) -> dict` returning
  `{"migrated": int, "thumbs": int, "missing_thumbs": int, "legacy_originals": int, "deleted_legacy": bool}`.

- [ ] **Step 1: Write the failing test**

```python
def _mk_legacy(root, trash):
    # legacy original at RECYCLE_BIN/PHOTOS/S95/2025/IMG_1.jpg + its md5(subpath) thumbs
    import hashlib
    sub = "S95/2025/IMG_1.jpg"
    op = os.path.join(root, "PHOTOS", "S95", "2025")
    os.makedirs(op)
    with open(os.path.join(op, "IMG_1.jpg"), "wb") as f: f.write(b"JPEGDATA")
    tdir = os.path.join(root, "_thumbs"); os.makedirs(tdir)
    h = hashlib.md5(sub.encode()).hexdigest()
    for name in (f"thumbs_hq_{h}.jpg", f"thumbs_{h}.jpg"):
        with open(os.path.join(tdir, name), "wb") as f: f.write(b"THUMB")
    os.makedirs(trash, exist_ok=True); os.makedirs(os.path.join(trash, "_thumbs"), exist_ok=True)

def test_migrate_moves_original_and_writes_meta():
    root = tempfile.mkdtemp(); trash = tempfile.mkdtemp()
    _mk_legacy(root, trash)
    res = m.migrate(root, trash)
    assert res["migrated"] == 1
    metas = [f for f in os.listdir(trash) if f.endswith(".meta.json")]
    assert len(metas) == 1
    meta = json.loads(open(os.path.join(trash, metas[0])).read())
    assert meta["original_path"] == "/mnt/data/PROMETHEUS/PHOTOS/S95/2025/IMG_1.jpg"
    # thumb carried into trash/_thumbs/<trash_name>.jpg
    tn = meta["trash_name"]
    assert os.path.exists(os.path.join(trash, "_thumbs", tn + ".jpg"))
    assert res["thumbs"] == 1
    # legacy dir deleted after full verify
    assert res["deleted_legacy"] is True
    assert not os.path.exists(root)

def test_migrate_keeps_legacy_when_incomplete(monkeypatch):
    root = tempfile.mkdtemp(); trash = tempfile.mkdtemp()
    _mk_legacy(root, trash)
    # force a move failure by making trash read-only mid-run
    orig_move = shutil.move
    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(m.shutil, "move", boom)
    res = m.migrate(root, trash)
    assert res["migrated"] == 0
    assert res["deleted_legacy"] is False
    assert os.path.exists(root)  # legacy preserved on failure
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_migrate_recycle_bin.py -k migrate_ -v`
Expected: FAIL (`migrate` not defined).

- [ ] **Step 3: Write minimal implementation**

```python
# append to scripts/migrate_recycle_bin.py
def migrate(recycle_root: str, trash_dir: str) -> dict:
    photos_dir = os.path.join(recycle_root, "PHOTOS")
    legacy_thumbs = os.path.join(recycle_root, "_thumbs")
    trash_thumbs = os.path.join(trash_dir, "_thumbs")
    os.makedirs(trash_thumbs, exist_ok=True)
    # Council hardening: refuse to run cross-device (would force copy+delete on a
    # near-full disk, risking a half-written original). Same fs => rename is atomic.
    if os.stat(recycle_root).st_dev != os.stat(trash_dir).st_dev:
        raise RuntimeError("legacy bin and .ares-trash are on different filesystems; abort")

    originals = []
    for dirpath, _dirs, files in os.walk(photos_dir):
        for fn in files:
            full = os.path.join(dirpath, fn)
            sub = os.path.relpath(full, photos_dir)  # e.g. S95/2025/IMG_1.jpg
            originals.append((full, sub))

    migrated = thumbs = missing = 0
    moved = []  # (trash_name, expected_size) for byte-level verify before delete
    for full, sub in originals:
        st = os.stat(full)
        original_path = reconstruct_original_path(sub)
        # idempotency: skip if an existing meta already claims this original
        if _already_migrated(trash_dir, original_path):
            continue
        trash_name = unique_trash_name(trash_dir, st.st_mtime, os.path.basename(sub))
        try:
            shutil.move(full, os.path.join(trash_dir, trash_name))
        except OSError:
            continue  # leave legacy intact; do not count as migrated
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
    # the trash at its recorded size before we delete the only remaining copy. A meta
    # pointing at a missing/truncated file must NOT pass the gate.
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_migrate_recycle_bin.py -v`
Expected: PASS (all tests, incl. Task 1's 4).

- [ ] **Step 5: Checkpoint** — do NOT run `main()` against real data yet; that happens in Task 7's rollout after review.

---

### Task 3: Photo-trash listing helper + route

**Files:**
- Create: `photos/trash_review.py`
- Modify: `app.py` (add `/api/photos/trash/list` route near the other `/api/photos/*` routes, ~line 5520)
- Test: `tests/test_trash_review.py`

**Interfaces:**
- Consumes: `system/recycling_bin.list_trash` (existing).
- Produces:
  - `PHOTO_EXTS: set[str]`
  - `list_photo_trash(trash_dir: str) -> list[dict]` → items `{trash_name, original_path, filename, trashed_at, age_str, purge_in, has_thumb}`, image/video only, newest first.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trash_review.py
import os, json, tempfile
from photos import trash_review as tr

def _seed(trash, name, original, is_photo=True):
    open(os.path.join(trash, name), "w").close()
    meta = {"original_path": original, "trash_name": name,
            "trashed_at": "2026-08-20T20:36:42.449319", "size": 10}
    with open(os.path.join(trash, name + ".meta.json"), "w") as f:
        json.dump(meta, f)

def test_list_photo_trash_filters_non_media():
    trash = tempfile.mkdtemp()
    _seed(trash, "20260820_1_IMG.JPG", "/mnt/data/PROMETHEUS/PHOTOS/x/IMG.JPG")
    _seed(trash, "20260820_2_note.txt", "/mnt/data/PROMETHEUS/PHOTOS/x/note.txt")
    items = tr.list_photo_trash(trash)
    names = [i["trash_name"] for i in items]
    assert "20260820_1_IMG.JPG" in names
    assert "20260820_2_note.txt" not in names
    assert items[0]["filename"] == "IMG.JPG"

def test_has_thumb_flag():
    trash = tempfile.mkdtemp(); os.makedirs(os.path.join(trash, "_thumbs"))
    _seed(trash, "20260820_1_IMG.JPG", "/mnt/data/PROMETHEUS/PHOTOS/x/IMG.JPG")
    open(os.path.join(trash, "_thumbs", "20260820_1_IMG.JPG.jpg"), "w").close()
    items = tr.list_photo_trash(trash)
    assert items[0]["has_thumb"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trash_review.py -v`
Expected: FAIL (`photos.trash_review` missing).

- [ ] **Step 3: Write minimal implementation**

```python
# photos/trash_review.py
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
```

- [ ] **Step 4: Add the route to app.py**

Add near line 5520 (by the other `/api/photos/*` routes):

```python
from photos import trash_review as _trash_review
from system.recycling_bin import TRASH_DIR as _TRASH_DIR

@app.route("/api/photos/trash/list")
@require_auth
def api_photos_trash_list():
    return jsonify(_trash_review.list_photo_trash(str(_TRASH_DIR)))
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_trash_review.py -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Smoke-test the route**

Run: `pct exec 101 -- systemctl restart ares && sleep 4 && curl -s -o /dev/null -w "%{http_code}\n" http://192.168.20.213:8080/api/photos/trash/list`
Expected: `401` (require_auth gates /api/* with 401, not a 302 — route wired, not a 500).

---

### Task 4: Trash thumbnail route (serve-or-generate)

**Files:**
- Modify: `photos/trash_review.py` (add `trash_thumb_path`, `ensure_thumb`)
- Modify: `app.py` (add `/api/photos/trash/thumb/<trash_name>` route)
- Test: `tests/test_trash_review.py` (add thumb tests)

**Interfaces:**
- Consumes: existing cold-path thumbnail generator in app.py. Reuse `_generate_thumb(src_path, dest_path)` if present; otherwise the route uses Pillow inline (see Step 3).
- Produces:
  - `trash_thumb_path(trash_dir: str, trash_name: str) -> str`
  - `original_file_path(trash_dir: str, trash_name: str) -> str` (the trashed media file itself)

- [ ] **Step 1: Write the failing test**

```python
def test_trash_thumb_path():
    trash = "/tmp/xbin"
    assert tr.trash_thumb_path(trash, "20260820_1_IMG.JPG") == \
        "/tmp/xbin/_thumbs/20260820_1_IMG.JPG.jpg"

def test_original_file_path():
    trash = "/tmp/xbin"
    assert tr.original_file_path(trash, "20260820_1_IMG.JPG") == \
        "/tmp/xbin/20260820_1_IMG.JPG"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trash_review.py -k "thumb_path or original_file" -v`
Expected: FAIL (functions undefined).

- [ ] **Step 3: Write minimal implementation**

```python
# add to photos/trash_review.py
def trash_thumb_path(trash_dir: str, trash_name: str) -> str:
    return os.path.join(trash_dir, "_thumbs", trash_name + ".jpg")

def original_file_path(trash_dir: str, trash_name: str) -> str:
    return os.path.join(trash_dir, trash_name)
```

Add the route to app.py (near Task 3's route):

```python
@app.route("/api/photos/trash/thumb/<path:trash_name>")
@require_auth
def api_photos_trash_thumb(trash_name):
    tdir = str(_TRASH_DIR)
    thumb = _trash_review.trash_thumb_path(tdir, trash_name)
    if not os.path.exists(thumb):
        src = _trash_review.original_file_path(tdir, trash_name)
        if not os.path.exists(src):
            return "not found", 404
        os.makedirs(os.path.dirname(thumb), exist_ok=True)
        try:
            from PIL import Image
            im = Image.open(src); im.thumbnail((475, 475))
            im.convert("RGB").save(thumb, "JPEG", quality=80)
        except Exception:
            return "thumb failed", 415  # e.g. video without a still — frontend shows a placeholder
    return send_file(thumb, mimetype="image/jpeg")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_trash_review.py -v`
Expected: PASS (all trash_review tests).

- [ ] **Step 5: Smoke-test** — after `systemctl restart ares`, the route returns 401 unauthenticated (wired, no 500).

---

### Task 5: Restore + purge routes

**Files:**
- Modify: `photos/trash_review.py` (add `purge_item`)
- Modify: `app.py` (add restore + purge routes)
- Test: `tests/test_trash_review.py` (add purge test)

**Interfaces:**
- Consumes: `system/recycling_bin.restore` (existing).
- Produces: `purge_item(trash_dir: str, trash_name: str) -> dict` → `{"success": bool, ...}`; deletes the file, its `.meta.json`, and its trash thumb.

- [ ] **Step 1: Write the failing test**

```python
def test_purge_removes_file_meta_and_thumb():
    trash = tempfile.mkdtemp(); os.makedirs(os.path.join(trash, "_thumbs"))
    _seed(trash, "20260820_1_IMG.JPG", "/mnt/data/PROMETHEUS/PHOTOS/x/IMG.JPG")
    open(os.path.join(trash, "_thumbs", "20260820_1_IMG.JPG.jpg"), "w").close()
    res = tr.purge_item(trash, "20260820_1_IMG.JPG")
    assert res["success"] is True
    assert not os.path.exists(os.path.join(trash, "20260820_1_IMG.JPG"))
    assert not os.path.exists(os.path.join(trash, "20260820_1_IMG.JPG.meta.json"))
    assert not os.path.exists(os.path.join(trash, "_thumbs", "20260820_1_IMG.JPG.jpg"))

def test_purge_missing_item():
    trash = tempfile.mkdtemp()
    assert tr.purge_item(trash, "nope.jpg")["success"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trash_review.py -k purge -v`
Expected: FAIL (`purge_item` undefined).

- [ ] **Step 3: Write minimal implementation**

```python
# add to photos/trash_review.py
def purge_item(trash_dir: str, trash_name: str) -> dict:
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
```

Add routes to app.py:

```python
@app.route("/api/photos/trash/restore", methods=["POST"])
@require_auth
def api_photos_trash_restore():
    tn = (request.json or {}).get("trash_name", "")
    from system.recycling_bin import restore as _restore
    res = _restore(tn)
    if res.get("success"):
        t = _trash_review.trash_thumb_path(str(_TRASH_DIR), tn)
        if os.path.exists(t):
            os.remove(t)
        _photo_cache["data"] = None; _month_cache["data"] = None
    return jsonify(res)

@app.route("/api/photos/trash/purge", methods=["POST"])
@require_auth
def api_photos_trash_purge():
    tn = (request.json or {}).get("trash_name", "")
    return jsonify(_trash_review.purge_item(str(_TRASH_DIR), tn))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_trash_review.py -v`
Expected: PASS (all).

- [ ] **Step 5: Smoke-test** — restart ares, both POST routes return 401 unauthenticated.

---

### Task 6: Future deletes carry a thumb

**Files:**
- Modify: `app.py` — `trash_photo()` (~line 3583)
- Test: manual (the existing trash flow already has coverage; this adds a best-effort copy)

**Interfaces:**
- Consumes: existing `trash_file()` result (returns `trash_name` on success) and live thumb at `static/thumbs/<hash>.jpg`.

- [ ] **Step 1: Read the current `trash_photo()` return**

Confirm `trash_file(orig_path)` returns a dict containing the created `trash_name` (check `system/recycling_bin.trash_file`). If the key differs, use the actual key.

- [ ] **Step 2: Add best-effort thumb copy**

In `trash_photo()`, after `result = trash_file(orig_path)` and the `if result.get("success"):` block, add:

```python
        # carry the live thumb so the Recycle Bin grid is instant
        try:
            tn = result.get("trash_name")
            live_thumb = os.path.join(_THUMB_DIR, name)  # name = "<hash>.jpg"
            if tn and os.path.exists(live_thumb):
                dst_dir = os.path.join(str(_TRASH_DIR), "_thumbs")
                os.makedirs(dst_dir, exist_ok=True)
                shutil.copy2(live_thumb, os.path.join(dst_dir, tn + ".jpg"))
        except Exception:
            pass  # non-fatal — thumb route regenerates on demand
```

- [ ] **Step 3: Verify** — restart ares, trash one test photo via the gallery, confirm `.ares-trash/_thumbs/<trash_name>.jpg` appears. Restore it afterward.

---

### Task 7: Recycle Bin review page + rollout

**Files:**
- Create: `templates/recycle_bin.html` (mirror `templates/dupes_review.html` structure)
- Modify: `app.py` (add `GET /photos/recycle` render route)
- Modify: `templates/home.html` (add one link/button in the Photos view header to `/photos/recycle`)

- [ ] **Step 1: Add the page route to app.py**

```python
@app.route("/photos/recycle")
@require_auth
def photos_recycle_page():
    return render_template("recycle_bin.html")
```

- [ ] **Step 2: Create `templates/recycle_bin.html`**

```html
<!doctype html><html><head><meta charset="utf-8">
<title>Recycle Bin</title>
<style>
  body{background:#111;color:#eee;font-family:system-ui;margin:0;padding:16px;}
  h1{font-size:18px;} .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px;}
  .card{background:#1b1b1b;border:1px solid #333;border-radius:8px;overflow:hidden;}
  .card img{width:100%;height:140px;object-fit:cover;background:#000;}
  .meta{padding:6px;font-size:11px;color:#aaa;} .fn{color:#ddd;word-break:break-all;}
  .row{display:flex;gap:6px;padding:6px;}
  button{flex:1;border:1px solid #444;background:#222;color:#eee;border-radius:6px;padding:6px;cursor:pointer;}
  .del{border-color:#a33;color:#f88;} .empty{color:#888;padding:40px;text-align:center;}
</style></head><body>
<h1>Recycle Bin <span id="count"></span></h1>
<div id="grid" class="grid"></div>
<script>
async function load(){
  const r = await fetch('/api/photos/trash/list'); const items = await r.json();
  document.getElementById('count').textContent = '('+items.length+')';
  const g = document.getElementById('grid');
  if(!items.length){ g.innerHTML='<div class="empty">Recycle bin is empty.</div>'; return; }
  g.innerHTML = items.map(function(it){
    return '<div class="card" data-tn="'+encodeURIComponent(it.trash_name)+'">'
      + '<img loading="lazy" src="/api/photos/trash/thumb/'+encodeURIComponent(it.trash_name)+'" '
      + 'onerror="this.style.opacity=0.3">'
      + '<div class="meta"><div class="fn">'+it.filename+'</div>'
      + '<div>'+it.age_str+' · purges in '+it.purge_in+'d</div></div>'
      + '<div class="row"><button onclick="act(this,\'restore\')">Restore</button>'
      + '<button class="del" onclick="act(this,\'purge\')">Delete</button></div></div>';
  }).join('');
}
async function act(btn, kind){
  const card = btn.closest('.card'); const tn = decodeURIComponent(card.dataset.tn);
  if(kind==='purge' && !confirm('Permanently delete '+tn+'?')) return;
  btn.disabled = true;
  const r = await fetch('/api/photos/trash/'+kind, {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({trash_name:tn})});
  const res = await r.json();
  if(res.success){ card.remove(); } else { alert(res.error||'failed'); btn.disabled=false; }
}
load();
</script></body></html>
```

- [ ] **Step 3: Add the entry link in `templates/home.html`**

Find the Photos view header (search `home.html` for the photos toolbar / an existing filter button block) and add:

```html
<a href="/photos/recycle" class="btn" title="Deleted photos">Recycle Bin</a>
```

Match the surrounding button classes so it visually fits the existing toolbar.

- [ ] **Step 4: Restart + manual verify (empty state)**

Run: `pct exec 101 -- systemctl restart ares`
Open `https://ares.tail3045df.ts.net/photos/recycle` — expect "Recycle bin is empty." (before migration) and no console errors.

---

### Task 7b: UX amendments — restore toast + Empty Bin (Outsider review)

**Files:**
- Modify: `photos/trash_review.py` (add `empty_bin`)
- Modify: `app.py` (add `/api/photos/trash/empty` route)
- Modify: `templates/recycle_bin.html` (restore toast + Empty Bin button)
- Test: `tests/test_trash_review.py` (add empty_bin test)

- [ ] **Step 1: Write the failing test**

```python
def test_empty_bin_purges_all_media():
    trash = tempfile.mkdtemp(); os.makedirs(os.path.join(trash, "_thumbs"))
    _seed(trash, "20260820_1_A.JPG", "/mnt/data/PROMETHEUS/PHOTOS/x/A.JPG")
    _seed(trash, "20260820_2_B.PNG", "/mnt/data/PROMETHEUS/PHOTOS/x/B.PNG")
    res = tr.empty_bin(trash)
    assert res["purged"] == 2
    assert tr.list_photo_trash(trash) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trash_review.py -k empty_bin -v`
Expected: FAIL (`empty_bin` undefined).

- [ ] **Step 3: Implement + wire route**

```python
# add to photos/trash_review.py
def empty_bin(trash_dir: str) -> dict:
    purged = 0
    for it in list_photo_trash(trash_dir):
        if purge_item(trash_dir, it["trash_name"]).get("success"):
            purged += 1
    return {"purged": purged}
```

```python
# app.py — near the other trash routes
@app.route("/api/photos/trash/empty", methods=["POST"])
@require_auth
def api_photos_trash_empty():
    return jsonify(_trash_review.empty_bin(str(_TRASH_DIR)))
```

- [ ] **Step 4: Frontend — restore toast + Empty Bin button**

In `templates/recycle_bin.html`, add a header button next to the `<h1>`:
```html
<button class="del" onclick="emptyBin()" style="float:right;max-width:180px">Empty bin</button>
```
And extend the script:
```javascript
function toast(msg){
  var t=document.createElement('div'); t.textContent=msg;
  t.style.cssText='position:fixed;bottom:16px;left:50%;transform:translateX(-50%);'
    +'background:#222;border:1px solid #444;color:#eee;padding:8px 14px;border-radius:8px;z-index:9';
  document.body.appendChild(t); setTimeout(function(){t.remove();}, 2500);
}
async function emptyBin(){
  if(!confirm('Permanently delete ALL photos in the recycle bin? This cannot be undone.')) return;
  const r = await fetch('/api/photos/trash/empty',{method:'POST'});
  const res = await r.json(); toast('Emptied bin — '+res.purged+' removed'); load();
}
```
In `act()`, on a successful **restore**, replace `card.remove();` with:
```javascript
    if(res.success){
      if(kind==='restore'){
        var where=(res.message||'').replace('Restored to: ','');
        var folder=where.split('/').slice(-3,-1).join('/');
        toast('Restored to '+ (folder||'your library'));
      }
      card.remove();
    } else { alert(res.error||'failed'); btn.disabled=false; }
```

- [ ] **Step 5: Run test + smoke-test**

Run: `python -m pytest tests/test_trash_review.py -v` → PASS.
Restart ares; `/api/photos/trash/empty` returns 401 unauthenticated.

---

### Task 8: Migration rollout (real 2,114 files — run manually, watched)

Isolated from the UI build so "mutate 2,114 real files" is never bundled with "ship code."
Do NOT start until Tasks 1–7b are green and the empty-state page renders clean.

- [ ] **Step 1: Backup the legacy dir (trivial insurance)**

```bash
cp -a /mnt/nvme/PROMETHEUS/PHOTOS/RECYCLE_BIN /mnt/nvme/PROMETHEUS/PHOTOS/RECYCLE_BIN.bak
du -sh /mnt/nvme/PROMETHEUS/PHOTOS/RECYCLE_BIN.bak   # expect ~8.4G
```
Keep `.bak` for a week after cutover, then remove it to reclaim the space.

- [ ] **Step 2: Live round-trip one throwaway photo (proves the restore contract)**

Trash one disposable test photo through the gallery UI, read the meta it wrote, and restore it:
```bash
ls -t /mnt/nvme/PROMETHEUS/PROJECTS/.ares-trash/*.meta.json | head -1 | xargs cat
```
Confirm `original_path` is `/mnt/data/PROMETHEUS/PHOTOS/...`, then Restore it in the UI and confirm the file returns to that location. (Pre-flight already confirmed this format across 47 metas; this is the final live proof.)

- [ ] **Step 3: Dry-run count**

```bash
python -c "import os; r='/mnt/nvme/PROMETHEUS/PHOTOS/RECYCLE_BIN/PHOTOS'; print('originals:', sum(len(f) for _,_,f in os.walk(r)))"
```
Expected: `originals: 2114`.

- [ ] **Step 4: Run the real migration (watch it)**

Run: `cd /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD && python -m scripts.migrate_recycle_bin`
Expected: `{'migrated': 2114, 'thumbs': 2114, 'missing_thumbs': 0, 'legacy_originals': 2114, 'deleted_legacy': True}`.
If `deleted_legacy` is False, the byte-verify gate failed — the legacy dir is intact; investigate before retrying (re-running is idempotent).

- [ ] **Step 5: Verify + review in UI**

- `df -h /mnt/nvme` — space reclaimed; `PHOTOS/RECYCLE_BIN` gone (only `.bak` remains).
- Reload `/photos/recycle` — 2,114 items render with thumbnails.
- Restore one item → confirm it returns to its original PHOTOS location + toast shows the folder.
- Purge one throwaway item → confirm file+meta+thumb all gone.
- After a week on the migrated bin: `rm -rf /mnt/nvme/PROMETHEUS/PHOTOS/RECYCLE_BIN.bak`.

---

## Self-Review

**Spec coverage:**
- Part A migration → Tasks 1, 2, 7 (rollout). ✓
- Reconstruct original path / thumb md5 mapping → Task 1. ✓
- Verify-before-delete gate → Task 2 (`migrate` deletes only when `migrated == len(originals)` and no files remain). ✓
- Thumb carry → Task 2. ✓
- List/thumb/restore/purge routes → Tasks 3,4,5. ✓
- Ongoing thumb-on-trash → Task 6. ✓
- Review page → Task 7. ✓
- YAGNI (no auto-purge changes, photos-only, no re-index) → respected; `purge_in` display-only. ✓

**Placeholder scan:** No TBD/TODO; every code step has full code. Task 7 Step 3 requires locating the Photos toolbar in `home.html` — the instruction says how to find it and what to add; acceptable (existing-file insertion point).

**Type consistency:** `trash_name` used consistently; `list_photo_trash`/`purge_item`/`trash_thumb_path`/`original_file_path` signatures match across tasks and routes. `TRASH_DIR` imported, never hardcoded.

**Risk:** `trash_file()` return-key for `trash_name` (Task 6 Step 1) is verified before use — if the key differs, the plan says use the actual key.
