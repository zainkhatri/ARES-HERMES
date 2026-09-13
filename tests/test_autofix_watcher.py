import os, sys, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "autofix"))
import watcher
import incident_store


def _tmp_path():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.unlink(path)
    return path


def test_collect_failed_units_parses_systemctl_json(monkeypatch):
    fake_output = json.dumps([
        {"unit": "ares-facescan.service", "load": "loaded", "active": "failed", "sub": "failed"},
        {"unit": "wol.service", "load": "loaded", "active": "failed", "sub": "failed"},
    ])
    monkeypatch.setattr(watcher, "_run_systemctl_failed", lambda: fake_output)
    units = watcher.collect_failed_units()
    assert len(units) == 2
    assert units[0]["unit"] == "ares-facescan.service"


def test_collect_failed_units_empty_on_no_failures(monkeypatch):
    monkeypatch.setattr(watcher, "_run_systemctl_failed", lambda: "[]")
    assert watcher.collect_failed_units() == []


def test_kill_switch_present_means_run_once_does_nothing():
    store_path = _tmp_path()
    store = incident_store.IncidentStore(store_path)
    ks_fd, ks_path = tempfile.mkstemp()
    os.close(ks_fd)  # file exists -> kill switch engaged
    try:
        count = watcher.run_once(store, ks_path)
        assert count == 0
        assert store.load()["incidents"] == []
    finally:
        os.unlink(ks_path)


def test_kill_switch_absent_file_is_not_engaged():
    # A genuinely missing kill-switch file means "not disabled" -- normal operation.
    store = incident_store.IncidentStore(_tmp_path())
    assert watcher._kill_switch_engaged("/tmp/definitely-does-not-exist-autofix-ks") is False


def test_kill_switch_stat_error_other_than_missing_fails_closed(monkeypatch):
    # spec requires: any error reading/stat'ing the kill switch (other than
    # "it just doesn't exist") = disabled=true. os.path.exists() would swallow
    # this into False (fail-open), so we assert the fail-closed stat path directly.
    def raise_permission_error(path):
        raise PermissionError("simulated stat failure")
    monkeypatch.setattr(watcher.os, "stat", raise_permission_error)
    assert watcher._kill_switch_engaged("/some/path") is True


def test_kill_switch_absent_allows_run(monkeypatch, tmp_path):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    ks_path = str(tmp_path / "does-not-exist")  # valid dir, missing file = not disabled
    monkeypatch.setattr(watcher, "_run_systemctl_failed", lambda: json.dumps(
        [{"unit": "ares-facescan.service", "load": "loaded", "active": "failed", "sub": "failed"}]
    ))
    monkeypatch.setattr(watcher, "_unit_log_excerpt", lambda unit: "Traceback ValueError: boom")
    monkeypatch.setattr(watcher, "collect_stale_jobs", lambda path: [])
    monkeypatch.setattr(watcher, "collect_alert_files", lambda paths: [])
    monkeypatch.setattr(watcher.triage, "triage", lambda unit, log: {"escalate": False, "reason": "test: skip"})
    count = watcher.run_once(store, ks_path)
    assert count == 1
    inc = store.load()["incidents"][0]
    assert inc["status"] == "triaged_skip"
    assert inc["source"] == "systemd_failed"
    assert inc["diagnosis"]["triage_reason"] == "test: skip"


def test_run_once_dedupes_against_pending_incident(monkeypatch, tmp_path):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    ks_path = str(tmp_path / "does-not-exist")
    monkeypatch.setattr(watcher, "_run_systemctl_failed", lambda: json.dumps(
        [{"unit": "ares-facescan.service", "load": "loaded", "active": "failed", "sub": "failed"}]
    ))
    monkeypatch.setattr(watcher, "_unit_log_excerpt", lambda unit: "Traceback ValueError: boom")
    monkeypatch.setattr(watcher, "collect_stale_jobs", lambda path: [])
    monkeypatch.setattr(watcher, "collect_alert_files", lambda paths: [])
    monkeypatch.setattr(watcher.triage, "triage", lambda unit, log: {"escalate": True, "reason": "test: escalate"})
    monkeypatch.setattr(watcher, "_launch_escalation", lambda *a, **k: None)
    first = watcher.run_once(store, ks_path)
    second = watcher.run_once(store, ks_path)
    assert first == 1
    assert second == 0  # same signature already pending ("escalated"), no re-escalation
    assert store.load()["incidents"][0]["status"] == "escalated"


def test_triage_and_route_escalates_and_launches(monkeypatch, tmp_path):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    iid = store.new_incident("sig-escalate", "systemd_failed", "some detail")
    monkeypatch.setattr(watcher.triage, "triage", lambda unit, log: {"escalate": True, "reason": "looks real"})
    launched = []
    monkeypatch.setattr(watcher, "_launch_escalation", lambda *a: launched.append(a))
    watcher._triage_and_route(store, iid, "sig-escalate", "systemd_failed", "some-unit", "some detail")
    inc = store.find_by_signature("sig-escalate")
    assert inc["status"] == "escalated"
    assert inc["diagnosis"]["triage_reason"] == "looks real"
    assert launched == [(iid, "sig-escalate", "systemd_failed", "some detail")]


def test_incident_title_prefers_error_line():
    excerpt = "Photo index: 35888 images\n\nTraceback (most recent call last):\n  File \"x.py\", line 244\nValueError: ambiguous truth value\n"
    title = watcher._incident_title("ares-facescan", excerpt)
    assert title == "ares-facescan — ValueError: ambiguous truth value"


def test_incident_title_falls_back_to_first_line_when_no_error_pattern():
    excerpt = "Main process exited, code=exited, status=1/FAILURE\nFailed with result 'exit-code'.\n"
    title = watcher._incident_title("ares-fleet", excerpt)
    assert title == "ares-fleet — Main process exited, code=exited, status=1/FAILURE"


def test_incident_title_falls_back_to_unit_when_excerpt_empty():
    assert watcher._incident_title("ares-fleet", "") == "ares-fleet"


def test_triage_and_route_skips_without_launching(monkeypatch, tmp_path):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    iid = store.new_incident("sig-skip", "systemd_failed", "some detail")
    monkeypatch.setattr(watcher.triage, "triage", lambda unit, log: {"escalate": False, "reason": "transient"})
    launched = []
    monkeypatch.setattr(watcher, "_launch_escalation", lambda *a: launched.append(a))
    watcher._triage_and_route(store, iid, "sig-skip", "systemd_failed", "some-unit", "some detail")
    inc = store.find_by_signature("sig-skip")
    assert inc["status"] == "triaged_skip"
    assert launched == []
