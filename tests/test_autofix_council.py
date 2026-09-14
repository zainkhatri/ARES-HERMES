import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "autofix"))
import council


class _FakeCompleted:
    def __init__(self, stdout):
        self.stdout = stdout


def test_ask_parses_last_line_json(monkeypatch):
    monkeypatch.setattr(council.subprocess, "run",
                         lambda *a, **k: _FakeCompleted('some noise\n{"approve": true, "verdict": "looks fine"}'))
    approved, verdict = council.ask("does this look ok?")
    assert approved is True
    assert verdict == "looks fine"


def test_ask_fails_closed_on_subprocess_error(monkeypatch):
    def raise_err(*a, **k):
        raise OSError("claude binary not found")
    monkeypatch.setattr(council.subprocess, "run", raise_err)
    approved, verdict = council.ask("prompt")
    assert approved is False
    assert "fail-closed" in verdict


def test_ask_fails_closed_on_unparseable_output(monkeypatch):
    monkeypatch.setattr(council.subprocess, "run", lambda *a, **k: _FakeCompleted("not json at all"))
    approved, verdict = council.ask("prompt")
    assert approved is False
    assert "fail-closed" in verdict


def test_panel_review_all_approve(monkeypatch):
    monkeypatch.setattr(council, "_ask_json", lambda prompt, **kw:
        {"what_happens": "mail flows again", "pros": ["alerts work"], "cons": ["none"]}
        if "what_happens" in prompt else {"approve": True, "verdict": "fine by me"})
    result = council.panel_review("<untrusted>ctx</untrusted>")
    assert result["approved"] is True
    assert len(result["votes"]) == 4
    assert {v["persona"] for v in result["votes"]} == {"Security", "Correctness", "Blast-Radius", "Pragmatist"}
    assert all(v["approve"] for v in result["votes"])
    assert result["summary"]["what_happens"] == "mail flows again"


def test_panel_review_3_of_4_threshold(monkeypatch):
    calls = {"n": 0}
    def fake_ask(prompt, **kw):
        if "what_happens" in prompt:
            return {"what_happens": "w", "pros": [], "cons": []}
        calls["n"] += 1
        return {"approve": calls["n"] != 1, "verdict": "v"}  # first persona rejects, rest approve
    monkeypatch.setattr(council, "_ask_json", fake_ask)
    result = council.panel_review("ctx")
    assert result["approved"] is True  # 3 of 4 approve -> passes


def test_panel_review_2_rejections_hold(monkeypatch):
    calls = {"n": 0}
    def fake_ask(prompt, **kw):
        if "what_happens" in prompt:
            return {"what_happens": "w", "pros": [], "cons": []}
        calls["n"] += 1
        return {"approve": calls["n"] > 2, "verdict": "v"}  # first two reject
    monkeypatch.setattr(council, "_ask_json", fake_ask)
    result = council.panel_review("ctx")
    assert result["approved"] is False


def test_panel_review_persona_error_counts_as_rejection(monkeypatch):
    def fake_ask(prompt, **kw):
        if "what_happens" in prompt:
            return {"what_happens": "w", "pros": [], "cons": []}
        return None  # every persona call errors
    monkeypatch.setattr(council, "_ask_json", fake_ask)
    result = council.panel_review("ctx")
    assert result["approved"] is False
    assert all(not v["approve"] for v in result["votes"])
    assert all("review failed" in v["verdict"] for v in result["votes"])


def test_panel_review_synthesis_failure_keeps_votes(monkeypatch):
    def fake_ask(prompt, **kw):
        if "what_happens" in prompt:
            return None  # synthesis fails
        return {"approve": True, "verdict": "v"}
    monkeypatch.setattr(council, "_ask_json", fake_ask)
    result = council.panel_review("ctx")
    assert result["approved"] is True
    assert result["summary"] == {}
