import os, sys, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "autofix"))
import watcher
import incident_store
import dedup


def _store(tmp_path):
    return incident_store.IncidentStore(str(tmp_path / "incidents.json"))


def _no_sources(monkeypatch):
    monkeypatch.setattr(watcher, "_run_systemctl_failed", lambda: "[]")
    monkeypatch.setattr(watcher, "collect_stale_jobs", lambda path: [])
    monkeypatch.setattr(watcher, "collect_alert_files", lambda paths: [])


def test_council_approved_auto_closes_when_source_healthy(monkeypatch, tmp_path):
    store = _store(tmp_path)
    ks = str(tmp_path / "no-ks")
    sig = dedup.normalize_signature("kg-nightly.service", "some old failure")
    iid = store.new_incident(sig, "systemd_failed", "old", title="kg-nightly stale")
    store.set_status(iid, "council_approved")
    _no_sources(monkeypatch)  # the unit is no longer failing
    watcher.run_once(store, ks)
    inc = store.find_by_signature(sig)
    assert inc["status"] == "resolved"
    assert inc["diagnosis"]["cleared_out_of_band"] is True


def test_recurring_needs_human_auto_closes_when_healthy(monkeypatch, tmp_path):
    store = _store(tmp_path)
    ks = str(tmp_path / "no-ks")
    sig = dedup.normalize_signature("ares-fleet.service", "old failure")
    iid = store.new_incident(sig, "systemd_failed", "old")
    store.set_status(iid, "recurring_needs_human")
    _no_sources(monkeypatch)
    watcher.run_once(store, ks)
    assert store.find_by_signature(sig)["status"] == "resolved"


def test_recommendation_ready_is_never_auto_closed(monkeypatch, tmp_path):
    # remote (EROS/ZEUS) recommendation-only findings have no pollable local
    # signal, so a quiet local poll must not silently resolve them.
    store = _store(tmp_path)
    ks = str(tmp_path / "no-ks")
    sig = dedup.normalize_signature("/mnt/.../zeus-ALERT", "remote guidance")
    iid = store.new_incident(sig, "alert_file", "guidance")
    store.set_status(iid, "recommendation_ready")
    _no_sources(monkeypatch)
    watcher.run_once(store, ks)
    assert store.find_by_signature(sig)["status"] == "recommendation_ready"


def test_still_failing_council_approved_is_not_closed(monkeypatch, tmp_path):
    store = _store(tmp_path)
    ks = str(tmp_path / "no-ks")
    unit, line = "kg-nightly.service", "Failed with result 'exit-code'."
    sig = dedup.normalize_signature(unit, line)
    iid = store.new_incident(sig, "systemd_failed", "old")
    store.set_status(iid, "council_approved")
    monkeypatch.setattr(watcher, "_run_systemctl_failed", lambda: json.dumps(
        [{"unit": unit, "load": "loaded", "active": "failed", "sub": "failed"}]))
    monkeypatch.setattr(watcher, "_unit_log_excerpt", lambda u: line)
    monkeypatch.setattr(watcher, "collect_stale_jobs", lambda path: [])
    monkeypatch.setattr(watcher, "collect_alert_files", lambda paths: [])
    watcher.run_once(store, ks)
    # still failing -> stays merge-ready, not auto-closed
    assert store.find_by_signature(sig)["status"] == "council_approved"
