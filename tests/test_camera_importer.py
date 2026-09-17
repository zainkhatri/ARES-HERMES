"""Tests for camera.importer — drain logic with fake client and tmp PHOTOS dir."""
import os
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

from camera.ccapi_client import ContentRef, CcapiNotAuthorized, CcapiNotReachable
from camera.ledger import Ledger, MAX_FAILS


def _make_ref(content_id, name, size, capture_time="2026:09:16 14:22:01"):
    return ContentRef(
        content_id=content_id,
        name=name,
        size=size,
        capture_time=capture_time,
        url="http://192.168.1.1:8080/ccapi/ver100/contents/sd/0/DCIM/100CANON/" + name,
        file_type="jpeg",
    )


def _fake_download(ref, dest_path):
    """Write ref.size bytes of zeros to dest_path."""
    with open(dest_path, "wb") as f:
        f.write(b"\x00" * ref.size)
    return ref.size


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    """Yields (client_mock, ledger, photos_root)."""
    photos = str(tmp_path / "PHOTOS")
    os.makedirs(photos)
    db = str(tmp_path / "ledger.db")
    ld = Ledger(db)
    monkeypatch.setenv("CAMERA_DEST_ROOT", photos)
    monkeypatch.setenv("CAMERA_IP", "192.168.1.1")

    client = MagicMock()
    client.ping.return_value = True
    client.download.side_effect = _fake_download

    yield client, ld, photos
    ld.close()


def test_drain_pulls_new_files(setup, monkeypatch):
    client, ledger, photos = setup
    refs = [_make_ref("id1", "IMG_0001.JPG", 10)]
    client.list_contents.return_value = refs

    with patch("camera.importer._DEST_ROOT", photos), \
         patch("camera.importer._reindex"):
        from camera import importer
        importer.drain(client, ledger)

    assert ledger.is_pulled("id1")
    pulled = [f for f in os.listdir(os.path.join(photos, "2026", "09"))
              if f.endswith(".JPG")]
    assert len(pulled) == 1


def test_drain_skips_already_pulled(setup):
    client, ledger, photos = setup
    refs = [_make_ref("id1", "IMG_0001.JPG", 10)]
    client.list_contents.return_value = refs
    ledger.mark_pulled("id1", "/already.jpg", 10)

    with patch("camera.importer._DEST_ROOT", photos), \
         patch("camera.importer._reindex"):
        from camera import importer
        importer.drain(client, ledger)

    client.download.assert_not_called()


def test_drain_skips_already_skipped(setup):
    client, ledger, photos = setup
    refs = [_make_ref("bad", "IMG_0001.JPG", 10)]
    client.list_contents.return_value = refs
    ledger.skip("bad")

    with patch("camera.importer._DEST_ROOT", photos), \
         patch("camera.importer._reindex"):
        from camera import importer
        importer.drain(client, ledger)

    client.download.assert_not_called()


def test_drain_per_file_isolation(setup):
    """A failure on file 1 must not prevent file 2 from being pulled."""
    client, ledger, photos = setup
    refs = [
        _make_ref("bad", "IMG_0001.JPG", 99),
        _make_ref("good", "IMG_0002.JPG", 5),
    ]
    client.list_contents.return_value = refs

    call_count = [0]
    def selective_download(ref, dest_path):
        call_count[0] += 1
        if ref.content_id == "bad":
            raise OSError("connection reset")
        return _fake_download(ref, dest_path)

    client.download.side_effect = selective_download

    with patch("camera.importer._DEST_ROOT", photos), \
         patch("camera.importer._reindex"):
        from camera import importer
        importer.drain(client, ledger)

    assert not ledger.is_pulled("bad")
    assert ledger.is_pulled("good")


def test_drain_size_mismatch_increments_fail(setup):
    client, ledger, photos = setup
    refs = [_make_ref("truncated", "IMG_0001.JPG", 100)]
    client.list_contents.return_value = refs

    def short_download(ref, dest_path):
        with open(dest_path, "wb") as f:
            f.write(b"\x00" * 10)  # only 10 bytes, expect 100
        return 10

    client.download.side_effect = short_download

    with patch("camera.importer._DEST_ROOT", photos), \
         patch("camera.importer._reindex"):
        from camera import importer
        importer.drain(client, ledger)

    assert ledger.fail_count("truncated") == 1
    assert not ledger.is_pulled("truncated")


def test_drain_three_failures_skips_file(setup):
    """After MAX_FAILS failures, file is permanently skipped."""
    client, ledger, photos = setup
    refs = [_make_ref("corrupt", "IMG_0001.JPG", 100)]
    client.list_contents.return_value = refs

    def always_short(ref, dest_path):
        with open(dest_path, "wb") as f:
            f.write(b"\x00")
        return 1

    client.download.side_effect = always_short

    with patch("camera.importer._DEST_ROOT", photos), \
         patch("camera.importer._reindex"):
        from camera import importer
        for _ in range(MAX_FAILS):
            importer.drain(client, ledger)

    assert ledger.is_skipped("corrupt")


def test_drain_collision_safe_naming(setup):
    """Two files with the same name get unique paths."""
    client, ledger, photos = setup
    ref1 = _make_ref("id1", "IMG_0001.JPG", 4)
    ref2 = _make_ref("id2", "IMG_0001.JPG", 4)
    client.list_contents.return_value = [ref1]

    with patch("camera.importer._DEST_ROOT", photos), \
         patch("camera.importer._reindex"):
        from camera import importer
        importer.drain(client, ledger)

    client.list_contents.return_value = [ref2]
    with patch("camera.importer._DEST_ROOT", photos), \
         patch("camera.importer._reindex"):
        importer.drain(client, ledger)

    dir_path = os.path.join(photos, "2026", "09")
    files = sorted(os.listdir(dir_path))
    assert len(files) == 2
    assert files[0] != files[1]


def test_drain_unknown_date_fallback(setup):
    """Bad capture_time lands in 'unknown/' not crashes."""
    client, ledger, photos = setup
    ref = _make_ref("id1", "IMG_0001.JPG", 3, capture_time="BADDATE")
    client.list_contents.return_value = [ref]

    with patch("camera.importer._DEST_ROOT", photos), \
         patch("camera.importer._reindex"):
        from camera import importer
        importer.drain(client, ledger)

    assert ledger.is_pulled("id1")
    assert os.path.isfile(os.path.join(photos, "unknown", "IMG_0001.JPG"))


def test_reindex_not_called_on_empty_drain(setup):
    client, ledger, photos = setup
    client.list_contents.return_value = []

    with patch("camera.importer._DEST_ROOT", photos), \
         patch("camera.importer._reindex") as mock_reindex:
        from camera import importer
        importer.drain(client, ledger)

    mock_reindex.assert_not_called()
