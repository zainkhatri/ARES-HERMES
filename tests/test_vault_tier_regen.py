"""Synthetic tests for on-demand vault tier regeneration. No real vault data is used."""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app  # noqa: E402
from PIL import Image  # noqa: E402

KEY = b"\x07" * 32


def _jpeg(w, h):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (120, 30, 200)).save(buf, "JPEG", quality=80)
    return buf.getvalue()


def _make_item(root, key, orig_plain):
    d = os.path.join(root, key)
    os.makedirs(d)
    for name, data in (("orig.enc", orig_plain), ("thumb.enc", _jpeg(475, 356)), ("hq.enc", _jpeg(800, 600))):
        with open(os.path.join(d, name), "wb") as fh:
            fh.write(app._vault_encrypt(KEY, data))
    return d


def test_regen_max_from_large_orig(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_VAULT_ENC_DIR", str(tmp_path))
    d = _make_item(str(tmp_path), "k1", _jpeg(4000, 3000))
    out = app._regen_vault_tier("k1", "thumb_max", KEY)
    assert out is not None
    im = Image.open(io.BytesIO(out))
    assert im.format == "JPEG" and max(im.size) == 2560
    with open(os.path.join(d, "max.enc"), "rb") as fh:
        assert app._vault_decrypt(KEY, fh.read()) == out
    assert not [n for n in os.listdir(d) if n.endswith(".tmp")]


def test_regen_preview_long_edge(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_VAULT_ENC_DIR", str(tmp_path))
    _make_item(str(tmp_path), "k2", _jpeg(3000, 4000))
    out = app._regen_vault_tier("k2", "thumb_preview", KEY)
    assert max(Image.open(io.BytesIO(out)).size) == 2048


def test_non_image_orig_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_VAULT_ENC_DIR", str(tmp_path))
    d = _make_item(str(tmp_path), "k3", b"\x00\x00\x00\x18ftypmp42 not an image")
    before = sorted(os.listdir(d))
    assert app._regen_vault_tier("k3", "thumb_max", KEY) is None
    assert sorted(os.listdir(d)) == before


def test_small_orig_not_upscaled(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_VAULT_ENC_DIR", str(tmp_path))
    _make_item(str(tmp_path), "k4", _jpeg(1000, 750))
    out = app._regen_vault_tier("k4", "thumb_max", KEY)
    assert Image.open(io.BytesIO(out)).size == (1000, 750)


def test_wrong_key_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_VAULT_ENC_DIR", str(tmp_path))
    d = _make_item(str(tmp_path), "k5", _jpeg(4000, 3000))
    assert app._regen_vault_tier("k5", "thumb_max", b"\x09" * 32) is None
    assert not os.path.exists(os.path.join(d, "max.enc"))


def test_unknown_tier_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_VAULT_ENC_DIR", str(tmp_path))
    _make_item(str(tmp_path), "k6", _jpeg(4000, 3000))
    assert app._regen_vault_tier("k6", "thumb_hq", KEY) is None


def test_eviction_never_drops_held_lock(monkeypatch):
    monkeypatch.setattr(app, "_VAULT_REGEN_LOCKS", app.OrderedDict())
    held = app._vault_regen_lock("held-key")
    assert held.acquire(blocking=False)
    try:
        for i in range(600):
            assert app._vault_regen_lock("flood-%d" % i) is not None
        assert app._vault_regen_lock("held-key") is held
        assert len(app._VAULT_REGEN_LOCKS) <= app._VAULT_REGEN_LOCKS_HARD
    finally:
        held.release()


def test_all_locks_held_hits_hard_cap(monkeypatch):
    monkeypatch.setattr(app, "_VAULT_REGEN_LOCKS", app.OrderedDict())
    locks = []
    for i in range(app._VAULT_REGEN_LOCKS_HARD):
        lk = app._vault_regen_lock("h-%d" % i)
        lk.acquire()
        locks.append(lk)
    try:
        assert app._vault_regen_lock("one-more") is None
    finally:
        for lk in locks:
            lk.release()


class _BusySem:
    def acquire(self, timeout=None):
        return False

    def release(self):
        raise AssertionError("release without acquire")


def test_semaphore_timeout_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_VAULT_ENC_DIR", str(tmp_path))
    monkeypatch.setattr(app, "_VAULT_REGEN_SEM", _BusySem())
    d = _make_item(str(tmp_path), "k7", _jpeg(4000, 3000))
    assert app._regen_vault_tier("k7", "thumb_max", KEY) is None
    assert not os.path.exists(os.path.join(d, "max.enc"))
