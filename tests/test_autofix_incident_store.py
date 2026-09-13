import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "autofix"))
import incident_store as store


def _tmp_path():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.unlink(path)  # store must create it fresh
    return path


def test_new_incident_creates_entry_with_new_status():
    s = store.IncidentStore(_tmp_path())
    iid = s.new_incident("unit:ares-facescan|err:ValueError", "systemd_failed", "exit-code 1")
    data = s.load()
    assert len(data["incidents"]) == 1
    inc = data["incidents"][0]
    assert inc["id"] == iid
    assert inc["status"] == "new"
    assert inc["signature"] == "unit:ares-facescan|err:ValueError"
    assert inc["diagnosis"] == {}


def test_write_diagnosis_never_touches_status():
    s = store.IncidentStore(_tmp_path())
    iid = s.new_incident("sig1", "systemd_failed", "detail")
    s.write_diagnosis(iid, diff="--- a\n+++ b\n", diff_hash="abc123")
    inc = s.find_by_signature("sig1")
    assert inc["status"] == "new"                 # unchanged
    assert inc["diagnosis"]["diff"] == "--- a\n+++ b\n"
    assert inc["diagnosis"]["diff_hash"] == "abc123"


def test_set_status_rejects_unknown_status():
    s = store.IncidentStore(_tmp_path())
    iid = s.new_incident("sig2", "systemd_failed", "detail")
    try:
        s.set_status(iid, "made_up_status")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_set_status_accepts_valid_transition():
    s = store.IncidentStore(_tmp_path())
    iid = s.new_incident("sig3", "systemd_failed", "detail")
    s.set_status(iid, "escalated")
    inc = s.find_by_signature("sig3")
    assert inc["status"] == "escalated"


def test_corrupt_file_raises_instead_of_silently_reinitializing():
    path = _tmp_path()
    with open(path, "w") as f:
        f.write("{not valid json")
    s = store.IncidentStore(path)
    try:
        s.load()
        assert False, "expected CorruptStoreError"
    except store.CorruptStoreError:
        pass


def test_find_by_signature_returns_none_when_missing():
    s = store.IncidentStore(_tmp_path())
    assert s.find_by_signature("nope") is None


def test_title_defaults_to_signature_when_not_given():
    s = store.IncidentStore(_tmp_path())
    s.new_incident("raw-hash-sig", "systemd_failed", "detail")
    assert s.find_by_signature("raw-hash-sig")["title"] == "raw-hash-sig"


def test_title_uses_given_human_readable_value():
    s = store.IncidentStore(_tmp_path())
    s.new_incident("raw-hash-sig2", "systemd_failed", "detail", title="ares-facescan: ValueError")
    assert s.find_by_signature("raw-hash-sig2")["title"] == "ares-facescan: ValueError"
