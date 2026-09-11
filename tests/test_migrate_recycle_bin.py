import os, tempfile, hashlib, json, shutil
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
    assert meta["trashed_at"].startswith("20")

def test_unique_trash_name_collision():
    d = tempfile.mkdtemp()
    n1 = m.unique_trash_name(d, 1700000000, "IMG.jpg")
    open(os.path.join(d, n1), "w").close()
    n2 = m.unique_trash_name(d, 1700000000, "IMG.jpg")
    assert n1 != n2

def _mk_legacy(root, trash):
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
    tn = meta["trash_name"]
    assert os.path.exists(os.path.join(trash, "_thumbs", tn + ".jpg"))
    assert res["thumbs"] == 1
    assert res["deleted_legacy"] is True
    assert not os.path.exists(root)

def test_migrate_keeps_legacy_when_incomplete(monkeypatch):
    root = tempfile.mkdtemp(); trash = tempfile.mkdtemp()
    _mk_legacy(root, trash)
    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(m.shutil, "move", boom)
    res = m.migrate(root, trash)
    assert res["migrated"] == 0
    assert res["deleted_legacy"] is False
    assert os.path.exists(root)

def test_migrate_byte_gate_blocks_delete_on_truncated_file(monkeypatch):
    # If a moved file ends up smaller than its recorded size, the gate must NOT delete legacy.
    root = tempfile.mkdtemp(); trash = tempfile.mkdtemp()
    _mk_legacy(root, trash)
    real_move = m.shutil.move
    def move_then_truncate(src, dst):
        real_move(src, dst)
        with open(dst, "wb") as f: f.write(b"")  # corrupt: 0 bytes vs recorded 8
    monkeypatch.setattr(m.shutil, "move", move_then_truncate)
    res = m.migrate(root, trash)
    assert res["deleted_legacy"] is False   # byte-verify gate caught the truncation
    assert os.path.exists(root)

def test_migrate_idempotent_rerun(monkeypatch):
    root = tempfile.mkdtemp(); trash = tempfile.mkdtemp()
    _mk_legacy(root, trash)
    m.migrate(root, trash)               # first run deletes legacy
    # recreate legacy with the SAME original to simulate a re-run against leftovers
    _mk_legacy(root, trash)
    res = m.migrate(root, trash)
    # the original_path already has a meta -> skipped, not double-migrated
    metas = [f for f in os.listdir(trash) if f.endswith(".meta.json")]
    assert len(metas) == 1
    assert res["migrated"] == 0
