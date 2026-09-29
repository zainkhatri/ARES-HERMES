import os, sys, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "autofix"))
import heartbeat


def test_write_ok_records_ts_and_last_ok(tmp_path):
    heartbeat.write("audit", ok=True, reason="", duration=12.5, hb_dir=str(tmp_path), now=1000)
    data = json.loads((tmp_path / "audit.json").read_text())
    assert data["agent"] == "audit"
    assert data["ok"] is True
    assert data["ts"] == 1000
    assert data["last_ok_ts"] == 1000
    assert data["duration"] == 12.5


def test_failed_write_keeps_previous_last_ok_ts(tmp_path):
    heartbeat.write("audit", ok=True, reason="", duration=1, hb_dir=str(tmp_path), now=1000)
    heartbeat.write("audit", ok=False, reason="claude CLI not found", duration=0, hb_dir=str(tmp_path), now=2000)
    data = json.loads((tmp_path / "audit.json").read_text())
    assert data["ok"] is False
    assert data["ts"] == 2000
    assert data["last_ok_ts"] == 1000
    assert data["reason"] == "claude CLI not found"


def test_first_ever_failure_has_no_last_ok(tmp_path):
    heartbeat.write("watcher", ok=False, reason="x", duration=0, hb_dir=str(tmp_path), now=5)
    data = json.loads((tmp_path / "watcher.json").read_text())
    assert data["last_ok_ts"] is None


def test_write_rejects_unknown_agent_name(tmp_path):
    try:
        heartbeat.write("../evil", ok=True, reason="", duration=0, hb_dir=str(tmp_path), now=1)
    except ValueError:
        assert not any(tmp_path.iterdir())
        return
    raise AssertionError("path-like agent name must be rejected")


def test_preflight_reports_missing_claude(monkeypatch):
    monkeypatch.setattr(heartbeat.shutil, "which", lambda name: None)
    reason = heartbeat.preflight()
    assert reason is not None
    assert "claude" in reason


def test_preflight_passes_when_claude_found(monkeypatch):
    monkeypatch.setattr(heartbeat.shutil, "which", lambda name: "/root/.local/bin/claude")
    assert heartbeat.preflight() is None


def test_status_classifies_ok_failed_stale_and_missing(tmp_path):
    day = 24 * 3600
    heartbeat.write("audit", ok=True, reason="", duration=1, hb_dir=str(tmp_path), now=10 * day)
    heartbeat.write("watcher", ok=False, reason="boom", duration=1, hb_dir=str(tmp_path), now=10 * day)
    rows = {r["agent"]: r for r in heartbeat.status(hb_dir=str(tmp_path), now=10 * day + 3600)}
    assert rows["audit"]["state"] == "ok"
    assert rows["watcher"]["state"] == "failed"
    stale = {r["agent"]: r for r in heartbeat.status(hb_dir=str(tmp_path), now=12 * day)}
    assert stale["audit"]["state"] == "stale"
    empty = {r["agent"]: r for r in heartbeat.status(hb_dir=str(tmp_path / "nope"), now=1)}
    assert empty["audit"]["state"] == "missing"
    assert empty["watcher"]["state"] == "missing"


def test_status_treats_corrupt_file_as_failed(tmp_path):
    (tmp_path / "audit.json").write_text("{not json")
    rows = {r["agent"]: r for r in heartbeat.status(hb_dir=str(tmp_path), now=1)}
    assert rows["audit"]["state"] == "failed"
    assert "unreadable" in rows["audit"]["reason"]


def test_watcher_main_reports_missing_claude_but_still_runs(tmp_path, monkeypatch):
    import watcher, incident_store
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    ran = []
    monkeypatch.setattr(watcher, "run_once", lambda s, ks: ran.append(1) or 2)
    monkeypatch.setattr(watcher.heartbeat, "preflight", lambda: "claude CLI not found on PATH (/usr/bin)")
    rc = watcher.main(store, kill_switch_path=str(tmp_path / "nope"), hb_dir=str(tmp_path / "hb"))
    assert rc == 1
    assert ran == [1]
    hb = json.loads((tmp_path / "hb" / "watcher.json").read_text())
    assert hb["ok"] is False
    assert "escalation cannot run" in hb["reason"]


def test_watcher_main_kill_switch_writes_paused(tmp_path, monkeypatch):
    import watcher, incident_store
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    ks = tmp_path / "ks"
    ks.write_text("")
    monkeypatch.setattr(watcher, "run_once", lambda s, k: (_ for _ in ()).throw(AssertionError("no run")))
    rc = watcher.main(store, kill_switch_path=str(ks), hb_dir=str(tmp_path / "hb"))
    assert rc == 0
    hb = json.loads((tmp_path / "hb" / "watcher.json").read_text())
    assert hb["reason"].startswith("paused")
