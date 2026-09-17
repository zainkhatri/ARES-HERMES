"""Tests for camera.status — the importer/dashboard IPC contract."""

import importlib
import os

import pytest


@pytest.fixture()
def status_mod(tmp_path, monkeypatch):
    """Load camera.status with STATUS_PATH redirected into a temp dir."""
    import camera.status as st
    monkeypatch.setattr(st, "STATUS_PATH", str(tmp_path / "camera_import_status.json"))
    return st


def test_read_default_when_missing(status_mod):
    d = status_mod.read_status()
    assert d["state"] == "idle"
    assert d["batch_total"] == 0 and d["batch_done"] == 0


def test_write_then_read_roundtrip(status_mod):
    status_mod.write_status("draining", batch_total=27, batch_done=12,
                            pulled_today=41, message="pulling")
    d = status_mod.read_status()
    assert d["state"] == "draining"
    assert d["batch_total"] == 27 and d["batch_done"] == 12
    assert d["pulled_today"] == 41 and d["message"] == "pulling"
    assert d["last_update"] > 0


def test_write_is_atomic_no_temp_left(status_mod):
    status_mod.write_status("done", batch_total=5, batch_done=5, message="done")
    d = os.path.dirname(status_mod.STATUS_PATH)
    leftovers = [f for f in os.listdir(d) if f.startswith(".camera_status.")]
    assert leftovers == []


def test_rejects_unknown_state(status_mod):
    with pytest.raises(AssertionError):
        status_mod.write_status("exploding")


def test_rejects_done_gt_total(status_mod):
    # batch_done may not exceed batch_total unless it drives the max itself
    with pytest.raises(AssertionError):
        status_mod.write_status("draining", batch_total=3, batch_done=5)


def test_corrupt_file_falls_back_to_default(status_mod):
    with open(status_mod.STATUS_PATH, "w") as f:
        f.write("{ not json")
    d = status_mod.read_status()
    assert d["state"] == "idle"
