"""Free-up-space safety: an iPhone photo may be deleted from the phone only when ARES holds the
exact file right now (iOS id -> content SHA -> path -> a non-empty file on disk)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app  # noqa: E402


def _setup(tmp_path, monkeypatch):
    good = tmp_path / "IMG_1.HEIC"
    good.write_bytes(b"\x00" * 100)
    empty = tmp_path / "IMG_2.HEIC"
    empty.write_bytes(b"")
    monkeypatch.setattr(app, "_ios_ids", {
        "ios-good": "sha-good", "ios-missing-file": "sha-gone", "ios-empty": "sha-empty",
        "ios-unknown-sha": "sha-never-stored"})
    monkeypatch.setattr(app, "_content_hashes", {
        "sha-good": str(good), "sha-gone": str(tmp_path / "deleted.HEIC"), "sha-empty": str(empty)})


def test_only_ids_whose_file_is_on_disk_are_verified(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    got = app._verified_ios_ids(["ios-good", "ios-missing-file", "ios-empty", "ios-unknown-sha", "ios-never-seen"])
    assert got == ["ios-good"]


def test_bad_input_verifies_nothing(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    assert app._verified_ios_ids(None) == []
    assert app._verified_ios_ids("ios-good") == []
    assert app._verified_ios_ids([None, 5, {"x": 1}, "ios-good"]) == ["ios-good"]


def test_input_is_capped(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    many = ["ios-good"] * (app.VERIFY_IOS_IDS_MAX + 50)
    assert len(app._verified_ios_ids(many)) == app.VERIFY_IOS_IDS_MAX
