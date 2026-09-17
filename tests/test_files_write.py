"""Unit tests for the confined write path (system/files_write.py) and the
directory-capable / gated recycling-bin additions.

Run: POOL_ROOT=/mnt/nvme/PROMETHEUS python -m pytest tests/test_files_write.py
The primitives take absolute paths, so they exercise against pytest tmp_path
directly; gate()/island logic is driven by monkeypatching safe_resolve.
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from system import files_write as fw
from system import recycling_bin as rb


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_valid_name_rejects_traversal_and_dotfiles():
    assert fw._valid_name("report.txt")
    for bad in ("", ".", "..", "a/b", "a\x00b", ".hidden", "x" * 256):
        assert not fw._valid_name(bad), bad


def test_bump_inserts_before_extension():
    assert fw._bump("report.txt", 1) == "report-2.txt"
    assert fw._bump("report.txt", 2) == "report-3.txt"
    assert fw._bump("noext", 1) == "noext-2"


def test_in_protected_island_matches_subtree_not_prefix(monkeypatch):
    monkeypatch.setattr(fw, "_ISLANDS", ("/pool/PHOTOS", "/pool/MORDOR"))
    assert fw._in_protected_island("/pool/PHOTOS")
    assert fw._in_protected_island("/pool/PHOTOS/2021/a.jpg")
    assert not fw._in_protected_island("/pool/PHOTOS-backup")   # os.sep guard
    assert not fw._in_protected_island("/pool/PROJECTS/x")


def test_gate_status_codes(monkeypatch):
    monkeypatch.setattr(fw, "_ISLANDS", ("/pool/PHOTOS",))
    monkeypatch.setattr(fw.files_api, "safe_resolve",
                        lambda rel: {"ok": "/pool/PROJECTS/x",
                                     "isl": "/pool/PHOTOS/a.jpg",
                                     "bad": None}.get(rel))
    assert fw.gate("ok") == ("ok", "/pool/PROJECTS/x")
    assert fw.gate("isl") == ("protected", "/pool/PHOTOS/a.jpg")
    assert fw.gate("bad") == ("notfound", None)


# ── make_dir ─────────────────────────────────────────────────────────────────

def test_make_dir_and_collision_bump(tmp_path):
    assert fw.make_dir(str(tmp_path), "docs") == "docs"
    assert (tmp_path / "docs").is_dir()
    assert fw.make_dir(str(tmp_path), "docs") == "docs-2"
    assert fw.make_dir(str(tmp_path), "docs") == "docs-3"


def test_make_dir_rejects_bad_name(tmp_path):
    with pytest.raises(AssertionError):
        fw.make_dir(str(tmp_path), "a/b")


# ── rename / move ────────────────────────────────────────────────────────────

def test_rename_item(tmp_path):
    f = tmp_path / "old.txt"
    f.write_text("hi")
    assert fw.rename_item(str(f), "new.txt") == "new.txt"
    assert (tmp_path / "new.txt").read_text() == "hi"
    assert not f.exists()


def test_rename_collision_bumps(tmp_path):
    (tmp_path / "a.txt").write_text("1")
    (tmp_path / "b.txt").write_text("2")
    assert fw.rename_item(str(tmp_path / "a.txt"), "b.txt") == "b-2.txt"
    assert (tmp_path / "b.txt").read_text() == "2"       # existing not clobbered
    assert (tmp_path / "b-2.txt").read_text() == "1"


def test_move_into_dir(tmp_path):
    src = tmp_path / "f.txt"
    src.write_text("x")
    dst = tmp_path / "sub"
    dst.mkdir()
    assert fw.move_into(str(src), str(dst)) == "f.txt"
    assert (dst / "f.txt").read_text() == "x"
    assert not src.exists()


def test_move_collision_bumps(tmp_path):
    src = tmp_path / "f.txt"
    src.write_text("new")
    dst = tmp_path / "sub"
    dst.mkdir()
    (dst / "f.txt").write_text("old")
    assert fw.move_into(str(src), str(dst)) == "f-2.txt"
    assert (dst / "f.txt").read_text() == "old"
    assert (dst / "f-2.txt").read_text() == "new"


def test_move_directory(tmp_path):
    src = tmp_path / "tree"
    (src / "inner").mkdir(parents=True)
    (src / "inner" / "deep.txt").write_text("z")
    dst = tmp_path / "dest"
    dst.mkdir()
    assert fw.move_into(str(src), str(dst)) == "tree"
    assert (dst / "tree" / "inner" / "deep.txt").read_text() == "z"


# ── upload ───────────────────────────────────────────────────────────────────

def test_write_upload_nested_creates_parents(tmp_path):
    stream = io.BytesIO(b"payload-bytes")
    name = fw.write_upload(str(tmp_path), "myfolder/sub/pic.bin", stream)
    assert name == "pic.bin"
    out = tmp_path / "myfolder" / "sub" / "pic.bin"
    assert out.read_bytes() == b"payload-bytes"
    # no leftover .part
    assert not any(p.name.endswith(".part") for p in (tmp_path / "myfolder" / "sub").iterdir())


def test_write_upload_collision_bumps(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"first")
    name = fw.write_upload(str(tmp_path), "a.txt", io.BytesIO(b"second"))
    assert name == "a-2.txt"
    assert (tmp_path / "a.txt").read_bytes() == b"first"
    assert (tmp_path / "a-2.txt").read_bytes() == b"second"


def test_write_upload_enforces_byte_cap_and_cleans_part(tmp_path):
    with pytest.raises(ValueError):
        fw.write_upload(str(tmp_path), "big.bin", io.BytesIO(b"x" * 100), max_bytes=10)
    assert list(tmp_path.iterdir()) == []                 # .part cleaned up, no leaf


def test_write_upload_rejects_traversal_segment(tmp_path):
    with pytest.raises(ValueError):
        fw.write_upload(str(tmp_path), "../escape.txt", io.BytesIO(b"x"))


# ── symlinked-parent attack (the TOCTOU the design hardens against) ───────────

def test_upload_refuses_symlinked_intermediate(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    base = tmp_path / "base"
    base.mkdir()
    os.symlink(str(outside), str(base / "link"))          # base/link -> outside
    # Uploading through the symlinked segment must NOT land in `outside`.
    with pytest.raises(OSError):
        fw.write_upload(str(base), "link/evil.txt", io.BytesIO(b"pwn"))
    assert not (outside / "evil.txt").exists()


# ── recycling bin: dir-capable trash + gated restore ─────────────────────────

def test_trash_path_file_and_restore(tmp_path, monkeypatch):
    trash = tmp_path / ".trash"
    trash.mkdir()
    monkeypatch.setattr(rb, "TRASH_DIR", trash)
    f = tmp_path / "doc.txt"
    f.write_text("keep")
    res = rb.trash_path(str(f))
    assert res["success"] and not f.exists()
    tn = res["trash_name"]
    assert (trash / f"{tn}.meta.json").exists()
    # gated restore back to original (parent allowed)
    out = rb.restore_gated(tn, gate=lambda p: True)
    assert out["success"]
    assert f.read_text() == "keep"


def test_trash_path_directory_size_none(tmp_path, monkeypatch):
    import json
    trash = tmp_path / ".trash"
    trash.mkdir()
    monkeypatch.setattr(rb, "TRASH_DIR", trash)
    d = tmp_path / "folder"
    (d / "a").mkdir(parents=True)
    (d / "a" / "x.txt").write_text("1")
    res = rb.trash_path(str(d))
    assert res["success"] and not d.exists()
    meta = json.loads((trash / f"{res['trash_name']}.meta.json").read_text())
    assert meta["is_dir"] is True and meta["size"] is None


def test_restore_gated_refuses_protected_destination(tmp_path, monkeypatch):
    trash = tmp_path / ".trash"
    trash.mkdir()
    monkeypatch.setattr(rb, "TRASH_DIR", trash)
    f = tmp_path / "doc.txt"
    f.write_text("data")
    tn = rb.trash_path(str(f))["trash_name"]
    out = rb.restore_gated(tn, gate=lambda p: False)      # destination "read-only"
    assert not out["success"] and "read-only" in out["error"]
    assert (trash / tn).exists()                          # item stays in trash


def test_restore_gated_autorenames_on_occupied(tmp_path, monkeypatch):
    trash = tmp_path / ".trash"
    trash.mkdir()
    monkeypatch.setattr(rb, "TRASH_DIR", trash)
    f = tmp_path / "doc.txt"
    f.write_text("original")
    tn = rb.trash_path(str(f))["trash_name"]
    f.write_text("new-file-same-name")                    # recreate at original path
    out = rb.restore_gated(tn, gate=lambda p: True)
    assert out["success"]
    assert (tmp_path / "doc-2.txt").read_text() == "original"
    assert f.read_text() == "new-file-same-name"          # existing not clobbered
