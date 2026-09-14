import os, sys, json, hashlib
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "autofix"))
import finalize
import incident_store


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


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
    monkeypatch.setattr(finalize, "_council_review", lambda *a: called.append(1) or (True, "n/a"))
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
    monkeypatch.setattr(finalize, "_council_review", lambda diff, reasoning, target: (True, "looks correct and safe"))
    status = finalize.finalize(iid, store_path=str(tmp_path / "incidents.json"),
                                result_path=str(result_path),
                                tmp_log_path=str(tmp_path / "no-log.log"))
    assert status == "council_approved"
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
    monkeypatch.setattr(finalize, "_council_review", lambda diff, reasoning, target: (False, "not confident, needs a human"))
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
    monkeypatch.setattr(finalize, "_council_review", lambda *a: (True, "ok"))
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
    def fake_review(reasoning, manual_steps, box):
        captured["box"] = box
        return True, "sound and safe recommendation"
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
    monkeypatch.setattr(finalize, "_council_review_recommendation", lambda *a: (False, "risks FAI/FCSF isolation"))
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
