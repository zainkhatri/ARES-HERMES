import os, sys, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "autofix"))
import audit
import incident_store
import pytest


@pytest.fixture(autouse=True)
def _claude_present(monkeypatch):
    monkeypatch.setattr(audit.heartbeat, "preflight", lambda: None)


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
    monkeypatch.setattr(audit, "_run_audit_session", lambda rp, lp, **kw: 0)
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
    monkeypatch.setattr(audit, "_run_audit_session", lambda rp, lp, **kw: 0)
    n = audit.run_once(store, kill_switch_path=ks_path, run_id="test2",
                        result_path=str(result_path), log_path=str(tmp_path / "audit.log"))
    assert n == 0
    assert store.load()["incidents"] == []


def test_missing_result_file_is_a_failed_run_not_zero_findings(tmp_path, monkeypatch):
    # 2026-09-24..28: session died instantly, no result file, run reported "0 findings" + exit 0
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    ks_path = str(tmp_path / "does-not-exist")
    monkeypatch.setattr(audit, "_run_audit_session", lambda rp, lp, **kw: 0)
    with pytest.raises(audit.AuditRunFailed) as exc:
        audit.run_once(store, kill_switch_path=ks_path, run_id="test3",
                       result_path=str(tmp_path / "never-written.json"), log_path=str(tmp_path / "audit.log"))
    assert "no result file" in str(exc.value)


def test_nonzero_session_exit_is_a_failed_run(tmp_path, monkeypatch):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    result_path = tmp_path / "audit-result.json"
    result_path.write_text("[]")
    log_path = tmp_path / "audit.log"
    log_path.write_text("timeout: failed to run command 'claude': No such file or directory\n")
    monkeypatch.setattr(audit, "_run_audit_session", lambda rp, lp, **kw: 127)
    with pytest.raises(audit.AuditRunFailed) as exc:
        audit.run_once(store, kill_switch_path=str(tmp_path / "nope"), run_id="t4",
                       result_path=str(result_path), log_path=str(log_path))
    assert "127" in str(exc.value)
    assert "failed to run command" in str(exc.value)


def test_preflight_failure_skips_session(tmp_path, monkeypatch):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    monkeypatch.setattr(audit.heartbeat, "preflight", lambda: "claude CLI not found on PATH (/usr/bin)")
    monkeypatch.setattr(audit, "_run_audit_session", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not run")))
    with pytest.raises(audit.AuditRunFailed) as exc:
        audit.run_once(store, kill_switch_path=str(tmp_path / "nope"), run_id="t5")
    assert "not found" in str(exc.value)


def test_main_writes_failed_heartbeat_and_exit_code(tmp_path, monkeypatch):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    monkeypatch.setattr(audit.heartbeat, "preflight", lambda: "claude CLI not found on PATH (/usr/bin)")
    rc = audit.main(store, kill_switch_path=str(tmp_path / "nope"), hb_dir=str(tmp_path / "hb"))
    assert rc == 1
    hb = json.loads((tmp_path / "hb" / "audit.json").read_text())
    assert hb["ok"] is False
    assert "not found" in hb["reason"]


def test_main_writes_ok_heartbeat_on_clean_empty_run(tmp_path, monkeypatch):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))

    def fake_session(rp, lp, **kw):
        open(rp, "w").write("[]")
        return 0
    monkeypatch.setattr(audit, "_run_audit_session", fake_session)
    rc = audit.main(store, kill_switch_path=str(tmp_path / "nope"), hb_dir=str(tmp_path / "hb"))
    assert rc == 0
    hb = json.loads((tmp_path / "hb" / "audit.json").read_text())
    assert hb["ok"] is True
    assert "0 finding" in hb["reason"]


def test_dedup_skips_finding_still_pending_from_a_prior_run(tmp_path, monkeypatch):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    ks_path = str(tmp_path / "does-not-exist")
    finding = {"title": "same issue again", "box": "ARES", "reasoning": "r", "manual_steps": "s"}
    result_path = tmp_path / "audit-result.json"
    result_path.write_text(json.dumps([finding]))
    monkeypatch.setattr(audit, "_run_audit_session", lambda rp, lp, **kw: 0)

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
