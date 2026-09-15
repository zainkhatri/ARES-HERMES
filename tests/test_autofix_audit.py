import os, sys, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "autofix"))
import audit
import incident_store


def test_kill_switch_engaged_means_no_findings_processed(tmp_path, monkeypatch):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    ks_fd, ks_path = tempfile.mkstemp()
    os.close(ks_fd)
    monkeypatch.setattr(audit, "_run_audit_session", lambda *a: (_ for _ in ()).throw(AssertionError("should not run")))
    try:
        n = audit.run_once(store, kill_switch_path=ks_path)
        assert n == 0
    finally:
        os.unlink(ks_path)


def test_processes_each_finding_through_finalize(tmp_path, monkeypatch):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    ks_path = str(tmp_path / "does-not-exist")
    result_path = tmp_path / "audit-result.json"
    findings = [
        {"title": "fleet-collect.sh lost +x bit", "box": "ARES", "reasoning": "permission stripped",
         "fix_title": "chmod +x", "diff": "--- a/x\n+++ b/x\n", "diff_hash": "h1",
         "base_snapshot_hash": "h2", "target_file": "x", "unit_name": ""},
        {"title": "ibt-db has no backup verification", "box": "EROS", "reasoning": "no restore test",
         "fix_title": "add restore-test cron", "manual_steps": "add a weekly pg_restore dry run"},
    ]
    result_path.write_text(json.dumps(findings))
    monkeypatch.setattr(audit, "_run_audit_session", lambda rp, lp, **kw: None)
    finalized = []
    monkeypatch.setattr(audit.finalize, "finalize", lambda iid, **kw: finalized.append(iid) or "council_approved")
    n = audit.run_once(store, kill_switch_path=ks_path, run_id="test1",
                        result_path=str(result_path), log_path=str(tmp_path / "audit.log"))
    assert n == 2
    assert len(finalized) == 2
    incidents = store.load()["incidents"]
    assert len(incidents) == 2
    assert incidents[0]["title"] == "[ARES] fleet-collect.sh lost +x bit"
    assert incidents[1]["title"] == "[EROS] ibt-db has no backup verification"
    assert incidents[0]["source"] == "audit"


def test_empty_findings_list_processes_nothing(tmp_path, monkeypatch):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    ks_path = str(tmp_path / "does-not-exist")
    result_path = tmp_path / "audit-result.json"
    result_path.write_text("[]")
    monkeypatch.setattr(audit, "_run_audit_session", lambda rp, lp, **kw: None)
    n = audit.run_once(store, kill_switch_path=ks_path, run_id="test2",
                        result_path=str(result_path), log_path=str(tmp_path / "audit.log"))
    assert n == 0
    assert store.load()["incidents"] == []


def test_unreadable_result_file_processes_nothing(tmp_path, monkeypatch):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    ks_path = str(tmp_path / "does-not-exist")
    monkeypatch.setattr(audit, "_run_audit_session", lambda rp, lp, **kw: None)
    n = audit.run_once(store, kill_switch_path=ks_path, run_id="test3",
                        result_path=str(tmp_path / "never-written.json"), log_path=str(tmp_path / "audit.log"))
    assert n == 0


def test_dedup_skips_finding_still_pending_from_a_prior_run(tmp_path, monkeypatch):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    ks_path = str(tmp_path / "does-not-exist")
    finding = {"title": "same issue again", "box": "ARES", "reasoning": "r", "manual_steps": "s"}
    result_path = tmp_path / "audit-result.json"
    result_path.write_text(json.dumps([finding]))
    monkeypatch.setattr(audit, "_run_audit_session", lambda rp, lp, **kw: None)

    def fake_finalize(iid, **kw):
        store.set_status(iid, "recommendation_ready")
        return "recommendation_ready"
    monkeypatch.setattr(audit.finalize, "finalize", fake_finalize)

    first = audit.run_once(store, kill_switch_path=ks_path, run_id="run1",
                            result_path=str(result_path), log_path=str(tmp_path / "a.log"))
    second = audit.run_once(store, kill_switch_path=ks_path, run_id="run2",
                             result_path=str(result_path), log_path=str(tmp_path / "b.log"))
    assert first == 1
    assert second == 0  # recommendation_ready still counts as "already have an answer for this"
