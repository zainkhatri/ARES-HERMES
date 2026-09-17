import os, sys, json, hashlib
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "autofix"))
import finalize
import incident_store


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _panel(approved, verdict="ok"):
    """Builds a fake council panel dict matching council.panel_review()'s shape."""
    vote = {"persona": "Security", "icon": "S", "approve": approved, "verdict": verdict}
    return {"votes": [vote] * 4, "approved": approved, "verdict": verdict,
            "summary": {"what_happens": "w", "pros": ["p"], "cons": ["c"]}}


def test_no_result_file_marks_diagnosis_timeout(tmp_path):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    iid = store.new_incident("sig1", "systemd_failed", "detail", title="unit1 — Error")
    store.set_status(iid, "escalated")
    status = finalize.finalize(iid, store_path=str(tmp_path / "incidents.json"),
                                result_path=str(tmp_path / "no-result.json"),
                                tmp_log_path=str(tmp_path / "no-log.log"))
    assert status == "diagnosis_timeout"
    inc = store.find_by_signature("sig1")
    assert inc["status"] == "diagnosis_timeout"


def test_unreadable_result_marks_diagnosis_timeout(tmp_path):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    iid = store.new_incident("sig2", "systemd_failed", "detail")
    result_path = tmp_path / "result.json"
    result_path.write_text("{not valid json")
    status = finalize.finalize(iid, store_path=str(tmp_path / "incidents.json"),
                                result_path=str(result_path),
                                tmp_log_path=str(tmp_path / "no-log.log"))
    assert status == "diagnosis_timeout"


def test_denylist_hit_blocks_before_council(tmp_path, monkeypatch):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    iid = store.new_incident("sig3", "systemd_failed", "detail")
    bad_diff = "--- a/vault/x.py\n+++ b/vault/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({
        "diff": bad_diff, "diff_hash": _hash(bad_diff),
        "base_snapshot_hash": _hash("orig"), "target_file": "vault/x.py",
        "reasoning": "fixed it", "fix_title": "fix vault thing",
    }))
    called = []
    monkeypatch.setattr(finalize, "_council_review", lambda *a: called.append(1) or _panel(True, "n/a"))
    status = finalize.finalize(iid, store_path=str(tmp_path / "incidents.json"),
                                result_path=str(result_path),
                                tmp_log_path=str(tmp_path / "no-log.log"))
    assert status == "council_held"
    assert called == []  # denylist blocks before council is ever invoked
    inc = store.find_by_signature("sig3")
    assert "denylisted" in inc["diagnosis"]["council_verdict"]


def test_clean_diff_reaches_council_and_gets_approved(tmp_path, monkeypatch):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    iid = store.new_incident("sig4", "systemd_failed", "detail")
    good_diff = "--- a/photos/ai_indexer.py\n+++ b/photos/ai_indexer.py\n@@ -1 +1 @@\n-a\n+b\n"
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({
        "diff": good_diff, "diff_hash": _hash(good_diff),
        "base_snapshot_hash": _hash("orig"), "target_file": "photos/ai_indexer.py",
        "unit_name": "ares-facescan",
        "reasoning": "fixed the numpy truthiness bug. it was ambiguous.",
        "fix_title": "fix numpy or[] crash",
    }))
    monkeypatch.setattr(finalize, "_council_review", lambda diff, reasoning, target: _panel(True, "looks correct and safe"))
    monkeypatch.setattr(finalize, "_auto_apply_if_ares", lambda store, iid, box: "resolved")
    status = finalize.finalize(iid, store_path=str(tmp_path / "incidents.json"),
                                result_path=str(result_path),
                                tmp_log_path=str(tmp_path / "no-log.log"))
    assert status == "resolved"
    inc = store.find_by_signature("sig4")
    assert inc["diagnosis"]["fix_title"] == "fix numpy or[] crash"
    assert inc["diagnosis"]["council_verdict"] == "looks correct and safe"
    assert inc["diagnosis"]["unit_name"] == "ares-facescan"


def test_clean_diff_but_council_holds(tmp_path, monkeypatch):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    iid = store.new_incident("sig5", "systemd_failed", "detail")
    good_diff = "--- a/photos/ai_indexer.py\n+++ b/photos/ai_indexer.py\n@@ -1 +1 @@\n-a\n+b\n"
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({
        "diff": good_diff, "diff_hash": _hash(good_diff),
        "base_snapshot_hash": _hash("orig"), "target_file": "photos/ai_indexer.py",
        "reasoning": "not sure this is right", "fix_title": "risky fix",
    }))
    monkeypatch.setattr(finalize, "_council_review", lambda diff, reasoning, target: _panel(False, "not confident, needs a human"))
    status = finalize.finalize(iid, store_path=str(tmp_path / "incidents.json"),
                                result_path=str(result_path),
                                tmp_log_path=str(tmp_path / "no-log.log"))
    assert status == "council_held"


def test_fix_title_falls_back_to_reasoning_first_sentence(tmp_path, monkeypatch):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    iid = store.new_incident("sig6", "systemd_failed", "detail")
    good_diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({
        "diff": good_diff, "diff_hash": _hash(good_diff),
        "base_snapshot_hash": _hash("orig"), "target_file": "x.py",
        "reasoning": "Fixed the off-by-one error. It was in the loop bound.",
    }))
    monkeypatch.setattr(finalize, "_council_review", lambda *a: _panel(True, "ok"))
    monkeypatch.setattr(finalize, "_auto_apply_if_ares", lambda store, iid, box: "resolved")
    finalize.finalize(iid, store_path=str(tmp_path / "incidents.json"),
                       result_path=str(result_path), tmp_log_path=str(tmp_path / "no-log.log"))
    inc = store.find_by_signature("sig6")
    assert inc["diagnosis"]["fix_title"] == "Fixed the off-by-one error"


def test_no_diff_but_manual_steps_reaches_recommendation_council(tmp_path, monkeypatch):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    iid = store.new_incident("sig-audit1", "audit", "detail")
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({
        "diff": "", "box": "EROS",
        "reasoning": "ibt-db has no automated backup verification",
        "manual_steps": "Add a weekly pg_restore dry-run cron on EROS",
        "fix_title": "Add backup verification for ibt-db",
    }))
    captured = {}
    def fake_review(reasoning, manual_steps, box, commands=None):
        captured["box"] = box
        return _panel(True, "sound and safe recommendation")
    monkeypatch.setattr(finalize, "_council_review_recommendation", fake_review)
    status = finalize.finalize(iid, store_path=str(tmp_path / "incidents.json"),
                                result_path=str(result_path), tmp_log_path=str(tmp_path / "no-log.log"))
    assert status == "recommendation_ready"
    assert captured["box"] == "EROS"
    inc = store.find_by_signature("sig-audit1")
    assert inc["diagnosis"]["manual_steps"] == "Add a weekly pg_restore dry-run cron on EROS"
    assert inc["diagnosis"]["council_verdict"] == "sound and safe recommendation"


def test_no_diff_and_recommendation_held_by_council(tmp_path, monkeypatch):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    iid = store.new_incident("sig-audit2", "audit", "detail")
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({
        "diff": "", "box": "EROS",
        "reasoning": "questionable suggestion",
        "manual_steps": "touch FCSF tenant config directly",
    }))
    monkeypatch.setattr(finalize, "_council_review_recommendation", lambda *a: _panel(False, "risks FAI/FCSF isolation"))
    status = finalize.finalize(iid, store_path=str(tmp_path / "incidents.json"),
                                result_path=str(result_path), tmp_log_path=str(tmp_path / "no-log.log"))
    assert status == "council_held"


def test_no_diff_and_no_manual_steps_is_diagnosis_timeout(tmp_path):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    iid = store.new_incident("sig-audit3", "audit", "detail")
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({"diff": "", "manual_steps": "", "reasoning": "found nothing actionable"}))
    status = finalize.finalize(iid, store_path=str(tmp_path / "incidents.json"),
                                result_path=str(result_path), tmp_log_path=str(tmp_path / "no-log.log"))
    assert status == "diagnosis_timeout"


def test_clean_commands_approved_by_council_become_council_approved(tmp_path, monkeypatch):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    iid = store.new_incident("sig-cmd1", "audit", "detail")
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({
        "diff": "", "box": "EROS", "reasoning": "postfix aliases db missing",
        "manual_steps": "run newaliases", "commands": ["newaliases", "postqueue -f"],
        "fix_title": "build postfix aliases db",
    }))
    monkeypatch.setattr(finalize, "_council_review_recommendation", lambda *a: _panel(True, "safe, non-business"))
    status = finalize.finalize(iid, store_path=str(tmp_path / "incidents.json"),
                                result_path=str(result_path), tmp_log_path=str(tmp_path / "no-log.log"))
    assert status == "council_approved"
    inc = store.find_by_signature("sig-cmd1")
    assert inc["diagnosis"]["commands"] == ["newaliases", "postqueue -f"]
    assert inc["diagnosis"]["commands_hash"]  # non-empty, computed


def test_denylisted_commands_blocked_before_council(tmp_path, monkeypatch):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    iid = store.new_incident("sig-cmd2", "audit", "detail")
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({
        "diff": "", "box": "EROS", "reasoning": "aggressive cleanup",
        "manual_steps": "wipe disk", "commands": ["rm -rf /"],
    }))
    called = []
    monkeypatch.setattr(finalize, "_council_review_recommendation", lambda *a: called.append(1) or _panel(True, "n/a"))
    status = finalize.finalize(iid, store_path=str(tmp_path / "incidents.json"),
                                result_path=str(result_path), tmp_log_path=str(tmp_path / "no-log.log"))
    assert status == "council_held"
    assert called == []  # command denylist blocks before council is ever invoked


def test_commands_rejected_by_council_stay_held_not_ready(tmp_path, monkeypatch):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    iid = store.new_incident("sig-cmd3", "audit", "detail")
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({
        "diff": "", "box": "EROS", "reasoning": "risky",
        "manual_steps": "do something", "commands": ["systemctl restart postfix"],
    }))
    monkeypatch.setattr(finalize, "_council_review_recommendation", lambda *a: _panel(False, "not confident"))
    status = finalize.finalize(iid, store_path=str(tmp_path / "incidents.json"),
                                result_path=str(result_path), tmp_log_path=str(tmp_path / "no-log.log"))
    assert status == "council_held"


def test_unattended_remote_exec_disabled_when_flag_file_absent(monkeypatch):
    monkeypatch.setattr(finalize.os.path, "exists", lambda p: False)
    assert finalize._unattended_remote_exec_enabled() is False


def test_eros_commands_approved_stay_council_approved_no_auto_execute_by_default(tmp_path, monkeypatch):
    """Default safety boundary: EROS/ZEUS never auto-execute unless the
    separate unattended-remote-exec flag has been deliberately enabled --
    council approval alone is not enough."""
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    iid = store.new_incident("sig-eros-auto", "audit", "detail")
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({
        "diff": "", "box": "EROS", "reasoning": "safe", "manual_steps": "run it",
        "commands": ["newaliases"],
    }))
    monkeypatch.setattr(finalize, "_council_review_recommendation", lambda *a: _panel(True, "safe"))
    monkeypatch.setattr(finalize, "_unattended_remote_exec_enabled", lambda: False)
    executed = []
    monkeypatch.setattr(finalize.apply, "run_commands", lambda *a, **k: executed.append(1) or "resolved")
    status = finalize.finalize(iid, store_path=str(tmp_path / "incidents.json"),
                                result_path=str(result_path), tmp_log_path=str(tmp_path / "no-log.log"))
    assert status == "council_approved"
    assert executed == []  # never invoked -- EROS waits for a human click


def test_eros_commands_auto_execute_when_unattended_flag_enabled(tmp_path, monkeypatch):
    """With the separate opt-in flag deliberately enabled, EROS ships the
    same as ARES does -- but only then."""
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    iid = store.new_incident("sig-eros-auto2", "audit", "detail")
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({
        "diff": "", "box": "EROS", "reasoning": "safe", "manual_steps": "run it",
        "commands": ["newaliases"],
    }))
    monkeypatch.setattr(finalize, "_council_review_recommendation", lambda *a: _panel(True, "safe"))
    monkeypatch.setattr(finalize, "_signed_off", lambda: True)
    monkeypatch.setattr(finalize, "_unattended_remote_exec_enabled", lambda: True)
    captured = {}
    monkeypatch.setattr(finalize.apply, "run_commands", lambda incident, box, run_fn: captured.update(box=box) or "resolved")
    status = finalize.finalize(iid, store_path=str(tmp_path / "incidents.json"),
                                result_path=str(result_path), tmp_log_path=str(tmp_path / "no-log.log"))
    assert status == "resolved"
    assert captured["box"] == "EROS"


def test_ares_commands_approved_auto_execute_no_click_needed(tmp_path, monkeypatch):
    """Explicit user decision, 2026-09-13: ARES ships unattended -- but only
    once the master sign-off flag is set."""
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    iid = store.new_incident("sig-ares-auto", "audit", "detail")
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({
        "diff": "", "box": "ARES", "reasoning": "cache cleanup", "manual_steps": "clear caches",
        "commands": ["npm cache clean --force"],
    }))
    monkeypatch.setattr(finalize, "_council_review_recommendation", lambda *a: _panel(True, "safe, regenerable cache"))
    monkeypatch.setattr(finalize, "_signed_off", lambda: True)
    captured = {}
    monkeypatch.setattr(finalize.apply, "run_commands", lambda incident, box, run_fn: captured.update(box=box) or "resolved")
    status = finalize.finalize(iid, store_path=str(tmp_path / "incidents.json"),
                                result_path=str(result_path), tmp_log_path=str(tmp_path / "no-log.log"))
    assert status == "resolved"
    assert captured["box"] == "ARES"
    assert store.find_by_signature("sig-ares-auto")["status"] == "resolved"


def test_ares_diff_approved_auto_applies_no_click_needed(tmp_path, monkeypatch):
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    iid = store.new_incident("sig-ares-diff-auto", "systemd_failed", "detail")
    good_diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({
        "diff": good_diff, "diff_hash": _hash(good_diff),
        "base_snapshot_hash": _hash("orig"), "target_file": "x.py", "unit_name": "ares-facescan",
        "reasoning": "fixed it",
    }))
    monkeypatch.setattr(finalize, "_council_review", lambda *a: _panel(True, "safe"))
    monkeypatch.setattr(finalize, "_signed_off", lambda: True)
    monkeypatch.setattr(finalize.apply, "apply_and_restart", lambda *a, **k: "resolved")
    status = finalize.finalize(iid, store_path=str(tmp_path / "incidents.json"),
                                result_path=str(result_path), tmp_log_path=str(tmp_path / "no-log.log"))
    assert status == "resolved"


def test_nothing_auto_executes_anywhere_before_sign_off_even_ares(tmp_path, monkeypatch):
    """The master gate: before SIGNED_OFF_FLAG exists, even an ARES finding
    with commands stays at council_approved -- council approval alone is
    never sufficient, matching the explicit user instruction 'dont execute
    until i sign off on it'."""
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    iid = store.new_incident("sig-not-signed-off", "audit", "detail")
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({
        "diff": "", "box": "ARES", "reasoning": "cache cleanup", "manual_steps": "clear caches",
        "commands": ["npm cache clean --force"],
    }))
    monkeypatch.setattr(finalize, "_council_review_recommendation", lambda *a: _panel(True, "safe"))
    monkeypatch.setattr(finalize, "_signed_off", lambda: False)
    executed = []
    monkeypatch.setattr(finalize.apply, "run_commands", lambda *a, **k: executed.append(1) or "resolved")
    status = finalize.finalize(iid, store_path=str(tmp_path / "incidents.json"),
                                result_path=str(result_path), tmp_log_path=str(tmp_path / "no-log.log"))
    assert status == "council_approved"
    assert executed == []


def _split_panel():
    """5-of-6 style panel: approved overall, but one dissenting vote."""
    yes = {"persona": "Security", "icon": "S", "approve": True, "verdict": "ok"}
    no = {"persona": "Blast-Radius", "icon": "B", "approve": False, "verdict": "too risky"}
    return {"votes": [yes] * 5 + [no], "approved": True, "verdict": "ok",
            "summary": {"what_happens": "w", "pros": [], "cons": []}}


def test_signed_off_but_split_vote_still_waits_for_merge_click(tmp_path, monkeypatch):
    """User rule 2026-09-15: auto-ship ONLY on unanimity. A 5/6 approval
    stays at council_approved even with the sign-off flag set."""
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    iid = store.new_incident("sig-split-vote", "audit", "detail")
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({
        "diff": "", "box": "ARES", "reasoning": "r", "manual_steps": "m",
        "commands": ["true"],
    }))
    monkeypatch.setattr(finalize, "_council_review_recommendation", lambda *a: _split_panel())
    monkeypatch.setattr(finalize, "_signed_off", lambda: True)
    monkeypatch.setattr(finalize.apply, "run_commands",
                         lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not execute on split vote")))
    status = finalize.finalize(iid, store_path=str(tmp_path / "incidents.json"),
                                result_path=str(result_path), tmp_log_path=str(tmp_path / "no-log.log"))
    assert status == "council_approved"


def test_signed_off_atlas_fix_ships_unanimous(tmp_path, monkeypatch):
    """User rule: unanimous ships, no exceptions -- atlas fixes auto-apply too."""
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    iid = store.new_incident("sig-atlas", "audit", "detail")
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({
        "diff": "--- a/store.py\n+++ b/store.py\n@@ -1 +1 @@\n-a\n+b\n", "diff_hash": "x",
        "base_snapshot_hash": "y", "target_file": "atlas/store.py", "target_repo": "atlas",
        "reasoning": "fts fix",
    }))
    monkeypatch.setattr(finalize, "_council_review", lambda *a: _panel(True, "ok"))
    monkeypatch.setattr(finalize, "_signed_off", lambda: True)
    monkeypatch.setattr(finalize.apply, "resolve_live_file_path", lambda *a, **k: str(tmp_path / "store.py"))
    monkeypatch.setattr(finalize.apply, "apply_and_restart", lambda *a, **k: "resolved")
    monkeypatch.setattr(finalize.apply, "git_commit_applied", lambda *a, **k: None)
    status = finalize.finalize(iid, store_path=str(tmp_path / "incidents.json"),
                                result_path=str(result_path), tmp_log_path=str(tmp_path / "no-log.log"))
    assert status == "resolved"


def test_signed_off_data_sensitive_fix_ships_unanimous(tmp_path, monkeypatch):
    """User rule: unanimous ships -- data-path command fixes auto-apply too."""
    store = incident_store.IncidentStore(str(tmp_path / "incidents.json"))
    iid = store.new_incident("sig-backup", "audit", "detail")
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({
        "diff": "", "box": "ARES", "reasoning": "r", "manual_steps": "m",
        "commands": ["rsync -a --delete /src /photos-backup"],
    }))
    monkeypatch.setattr(finalize, "_council_review_recommendation", lambda *a: _panel(True, "ok"))
    monkeypatch.setattr(finalize, "_signed_off", lambda: True)
    monkeypatch.setattr(finalize.apply, "run_commands", lambda *a, **k: "resolved")
    status = finalize.finalize(iid, store_path=str(tmp_path / "incidents.json"),
                                result_path=str(result_path), tmp_log_path=str(tmp_path / "no-log.log"))
    assert status == "resolved"
