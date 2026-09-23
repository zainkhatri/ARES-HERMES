import os, sys, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "autofix"))
import watcher
import incident_store
import dedup


def _store(tmp_path):
    return incident_store.IncidentStore(str(tmp_path / "incidents.json"))


# ---- recurrence_context (store layer) -------------------------------------

def test_recurrence_context_empty_store(tmp_path):
    ctx = _store(tmp_path).recurrence_context("sig-none")
    assert ctx == {"skip_count": 0, "last_status": None}


def test_recurrence_context_counts_skips_and_reports_last_status(tmp_path):
    store = _store(tmp_path)
    for _ in range(3):
        iid = store.new_incident("sig-x", "systemd_failed", "d")
        store.set_status(iid, "triaged_skip")
    # a newer, non-skip incident becomes the most recent
    iid = store.new_incident("sig-x", "systemd_failed", "d")
    store.set_status(iid, "resolved")
    ctx = store.recurrence_context("sig-x")
    assert ctx["skip_count"] == 3          # only the triaged_skip ones count
    assert ctx["last_status"] == "resolved"  # most-recent match wins


# ---- ratchet in _triage_and_route (repeat-skip) ----------------------------

def test_triage_and_route_promotes_to_needs_human_over_threshold(monkeypatch, tmp_path):
    store = _store(tmp_path)
    iid = store.new_incident("sig-skip", "dashboard_job", "detail")
    monkeypatch.setattr(watcher.triage, "triage", lambda unit, log: {"escalate": False, "reason": "looks transient"})
    watcher._triage_and_route(store, iid, "sig-skip", "dashboard_job", "zeus-horcrux", "detail",
                              skip_count=watcher.SKIP_RATCHET_N)
    inc = store.find_by_signature("sig-skip")
    assert inc["status"] == "recurring_needs_human"
    assert inc["diagnosis"]["skipped_n_times"] == watcher.SKIP_RATCHET_N


def test_triage_and_route_stays_skip_below_threshold(monkeypatch, tmp_path):
    store = _store(tmp_path)
    iid = store.new_incident("sig-skip", "dashboard_job", "detail")
    monkeypatch.setattr(watcher.triage, "triage", lambda unit, log: {"escalate": False, "reason": "transient"})
    watcher._triage_and_route(store, iid, "sig-skip", "dashboard_job", "zeus-horcrux", "detail",
                              skip_count=watcher.SKIP_RATCHET_N - 1)
    assert store.find_by_signature("sig-skip")["status"] == "triaged_skip"


def test_triage_and_route_default_skip_count_is_zero(monkeypatch, tmp_path):
    # backwards-compatible: callers that omit skip_count behave as before
    store = _store(tmp_path)
    iid = store.new_incident("sig-skip", "systemd_failed", "detail")
    monkeypatch.setattr(watcher.triage, "triage", lambda unit, log: {"escalate": False, "reason": "transient"})
    watcher._triage_and_route(store, iid, "sig-skip", "systemd_failed", "some-unit", "detail")
    assert store.find_by_signature("sig-skip")["status"] == "triaged_skip"


# ---- ratchet in run_once (regression + repeat-skip integration) ------------

def _fail_one_unit(monkeypatch, unit="zeus-horcrux.service", first_line="Failed with result 'exit-code'."):
    monkeypatch.setattr(watcher, "_run_systemctl_failed", lambda: json.dumps(
        [{"unit": unit, "load": "loaded", "active": "failed", "sub": "failed"}]))
    monkeypatch.setattr(watcher, "_unit_log_excerpt", lambda u: first_line)
    monkeypatch.setattr(watcher, "collect_stale_jobs", lambda path: [])
    monkeypatch.setattr(watcher, "collect_alert_files", lambda paths: [])


def test_run_once_regression_after_resolved_routes_to_needs_human(monkeypatch, tmp_path):
    store = _store(tmp_path)
    ks = str(tmp_path / "no-ks")
    unit, line = "zeus-horcrux.service", "Failed with result 'exit-code'."
    sig = dedup.normalize_signature(unit, line)
    prior = store.new_incident(sig, "systemd_failed", "old", title="prior")
    store.set_status(prior, "resolved")
    _fail_one_unit(monkeypatch, unit, line)
    # triage must NOT be consulted on a regression
    def _boom(*a, **k):
        raise AssertionError("triage should not run on a regression")
    monkeypatch.setattr(watcher.triage, "triage", _boom)
    created = watcher.run_once(store, ks)
    assert created == 1
    latest = store.load()["incidents"][-1]
    assert latest["status"] == "recurring_needs_human"
    assert latest["diagnosis"]["regressed_from"] == prior


def test_run_once_repeat_skip_promotes_to_needs_human(monkeypatch, tmp_path):
    store = _store(tmp_path)
    ks = str(tmp_path / "no-ks")
    unit, line = "zeus-horcrux.service", "Failed with result 'exit-code'."
    sig = dedup.normalize_signature(unit, line)
    for _ in range(watcher.SKIP_RATCHET_N):
        iid = store.new_incident(sig, "systemd_failed", "old")
        store.set_status(iid, "triaged_skip")
    _fail_one_unit(monkeypatch, unit, line)
    monkeypatch.setattr(watcher.triage, "triage", lambda u, l: {"escalate": False, "reason": "keeps skipping"})
    created = watcher.run_once(store, ks)
    assert created == 1
    assert store.load()["incidents"][-1]["status"] == "recurring_needs_human"
