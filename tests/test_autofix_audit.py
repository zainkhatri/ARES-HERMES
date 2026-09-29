import os, sys, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "autofix"))
import audit
import incident_store
import pytest


@pytest.fixture(autouse=True)
def _claude_present(monkeypatch):
    monkeypatch.setattr(audit.heartbeat, "preflight", lambda: None)


@pytest.fixture(autouse=True)
def _no_real_claude_session(monkeypatch):
    # a test that forgets to stub the session must fail, never launch a real
    # 90-min `claude -p` fleet audit (happened once, 2026-09-28)
    def _refuse(*a, **kw):
        raise AssertionError("test tried to start a real claude audit session -- stub _run_audit_session")
    monkeypatch.setattr(audit, "_run_audit_session", _refuse)


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


def _pending_finalize(store):
    def fake(iid, **kw):
        store.set_status(iid, "recommendation_ready")
        return "recommendation_ready"
    return fake


def test_same_unit_with_reworded_title_dedups(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "_run_audit_session", lambda rp, lp, **kw: 0)
    # 9/18-9/20: one fleet-collect.sh outage filed 3x because the LLM worded the title differently each day
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    monkeypatch.setattr(audit.finalize, "finalize", _pending_finalize(store))
    titles = ["ares-fleet.service crashes every 60s: fleet-collect.sh missing execute bit",
              "ares-fleet.service crash-loops (203/EXEC) -- fleet-collect.sh lost its execute bit"]
    counts = []
    for n, title in enumerate(titles):
        rp = tmp_path / f"r{n}.json"
        rp.write_text(json.dumps([{"title": title, "box": "ARES", "unit_name": "ares-fleet", "manual_steps": "x"}]))
        counts.append(audit.run_once(store, kill_switch_path=str(tmp_path / "nope"), run_id=f"d{n}",
                                     result_path=str(rp), log_path=str(tmp_path / f"l{n}.log")))
    assert counts == [1, 0]
    assert len(store.load()["incidents"]) == 1


def test_same_target_file_dedups_when_no_unit(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "_run_audit_session", lambda rp, lp, **kw: 0)
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    monkeypatch.setattr(audit.finalize, "finalize", _pending_finalize(store))
    for n, title in enumerate(["stale IP in elite_picks.py", "elite_picks.py still points at VM 300"]):
        rp = tmp_path / f"r{n}.json"
        rp.write_text(json.dumps([{"title": title, "box": "ARES", "target_file": "elite_picks.py", "manual_steps": "x"}]))
        audit.run_once(store, kill_switch_path=str(tmp_path / "nope"), run_id=f"t{n}",
                       result_path=str(rp), log_path=str(tmp_path / f"l{n}.log"))
    assert len(store.load()["incidents"]) == 1


def test_different_units_on_same_box_stay_separate(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "_run_audit_session", lambda rp, lp, **kw: 0)
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    monkeypatch.setattr(audit.finalize, "finalize", _pending_finalize(store))
    rp = tmp_path / "r.json"
    rp.write_text(json.dumps([{"title": "same words", "box": "ARES", "unit_name": "a", "manual_steps": "x"},
                              {"title": "same words", "box": "ARES", "unit_name": "b", "manual_steps": "x"}]))
    n = audit.run_once(store, kill_switch_path=str(tmp_path / "nope"), run_id="s",
                       result_path=str(rp), log_path=str(tmp_path / "l.log"))
    assert n == 2


def test_subject_key_wins_over_title_for_recommendations(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "_run_audit_session", lambda rp, lp, **kw: 0)
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    monkeypatch.setattr(audit.finalize, "finalize", _pending_finalize(store))
    for n, (title, subj) in enumerate([("EROS mail stuck", "/etc/aliases"), ("postfix aliases.db missing", "/etc/Aliases")]):
        rp = tmp_path / f"r{n}.json"
        rp.write_text(json.dumps([{"title": title, "box": "EROS", "subject": subj, "manual_steps": "x"}]))
        audit.run_once(store, kill_switch_path=str(tmp_path / "nope"), run_id=f"s{n}",
                       result_path=str(rp), log_path=str(tmp_path / f"l{n}.log"))
    assert len(store.load()["incidents"]) == 1
