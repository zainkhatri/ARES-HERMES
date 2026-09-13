import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "autofix"))
import watcher
import incident_store
import denylist


def test_synthetic_failure_creates_incident_and_bad_diff_is_caught(tmp_path):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    ks_path = str(tmp_path / "no-kill-switch")

    # 1. Simulate the watcher seeing a failed synthetic unit.
    fake_systemctl_output = '[{"unit": "autofix-synthetic-test.service", "load": "loaded", "active": "failed", "sub": "failed"}]'
    orig_run = watcher._run_systemctl_failed
    orig_excerpt = watcher._unit_log_excerpt
    orig_triage = watcher.triage.triage
    orig_launch = watcher._launch_escalation
    watcher._run_systemctl_failed = lambda: fake_systemctl_output
    watcher._unit_log_excerpt = lambda unit: "Traceback (most recent call last):\nValueError: synthetic failure for testing"
    watcher.triage.triage = lambda unit, log: {"escalate": True, "reason": "synthetic: looks real"}
    launched = []
    watcher._launch_escalation = lambda *a: launched.append(a)
    try:
        created = watcher.run_once(store, ks_path, host_crons_path=str(tmp_path / "no-crons.json"), alert_paths=())
    finally:
        watcher._run_systemctl_failed = orig_run
        watcher._unit_log_excerpt = orig_excerpt
        watcher.triage.triage = orig_triage
        watcher._launch_escalation = orig_launch

    assert created == 1
    incidents = store.load()["incidents"]
    assert incidents[0]["status"] == "escalated"
    assert incidents[0]["source"] == "systemd_failed"
    assert len(launched) == 1   # confirms escalate.sh would have been invoked exactly once

    # 2. Simulate a (deliberately bad) proposed diff that touches the vault --
    #    confirm the mechanical denylist catches it before council ever would.
    bad_diff = "--- a/vault/crypto_core.py\n+++ b/vault/crypto_core.py\n@@ -1 +1 @@\n-x\n+y\n"
    is_clean, violations = denylist.check_diff_paths(bad_diff)
    assert is_clean is False
    assert "vault/crypto_core.py" in violations
