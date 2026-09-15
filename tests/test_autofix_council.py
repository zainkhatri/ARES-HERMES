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


def _fake_ask_factory(opening_votes, final_votes, synthesis=None):
    """Builds a fake _ask_json aware of the two-round court shape:
    round-1 prompts ask for an 'OPENING opinion', round-2 for 'DELIBERATE',
    synthesis for 'what_happens'."""
    state = {"open": 0, "final": 0}
    def fake_ask(prompt, **kw):
        if "what_happens" in prompt:
            return synthesis if synthesis is not None else {"simple_explanation": "s", "what_happens": "w", "pros": [], "cons": []}
        if "OPENING opinion" in prompt:
            i = state["open"]; state["open"] += 1
            v = opening_votes[i]
            return None if v is None else {"approve": v, "opinion": f"opening {i}"}
        if "DELIBERATE" in prompt:
            i = state["final"]; state["final"] += 1
            v = final_votes[i]
            return None if v is None else {"approve": v, "verdict": f"final {i}", "reply": f"I address you, member {i}"}
        raise AssertionError("unexpected prompt")
    return fake_ask


def test_panel_review_all_approve(monkeypatch):
    monkeypatch.setattr(council, "_ask_json", _fake_ask_factory([True]*4, [True]*4,
        synthesis={"simple_explanation": "s", "what_happens": "mail flows again", "pros": ["alerts work"], "cons": ["none"]}))
    result = council.panel_review("<untrusted>ctx</untrusted>")
    assert result["approved"] is True
    assert len(result["votes"]) == 4
    assert {v["persona"] for v in result["votes"]} == {"Security", "Correctness", "Blast-Radius", "Pragmatist"}
    assert all(v["approve"] for v in result["votes"])
    assert result["summary"]["what_happens"] == "mail flows again"
    # discussion transcript: 4 openings + 4 replies, replies address the bench
    assert len(result["discussion"]) == 8
    assert [d["round"] for d in result["discussion"]] == [1, 1, 1, 1, 2, 2, 2, 2]


def test_panel_review_final_votes_decide_3_of_4(monkeypatch):
    # opening 1 rejects but changes mind in deliberation -> final 4/4
    monkeypatch.setattr(council, "_ask_json", _fake_ask_factory([False, True, True, True], [True]*4))
    result = council.panel_review("ctx")
    assert result["approved"] is True


def test_panel_review_2_final_rejections_hold(monkeypatch):
    monkeypatch.setattr(council, "_ask_json", _fake_ask_factory([True]*4, [False, False, True, True]))
    result = council.panel_review("ctx")
    assert result["approved"] is False  # 2/4 final < 3-of-4 threshold


def test_panel_review_persona_error_counts_as_rejection(monkeypatch):
    monkeypatch.setattr(council, "_ask_json", _fake_ask_factory([None]*4, [True]*4))
    result = council.panel_review("ctx")
    assert result["approved"] is False
    assert all(not v["approve"] for v in result["votes"])
    assert all("review failed" in v["verdict"] for v in result["votes"])


def test_panel_review_round2_error_keeps_round1_vote(monkeypatch):
    # openings all approve; every deliberation call fails -> final votes stay approve
    monkeypatch.setattr(council, "_ask_json", _fake_ask_factory([True]*4, [None]*4))
    result = council.panel_review("ctx")
    assert result["approved"] is True
    assert all(v["approve"] for v in result["votes"])
    assert any("stood by their opening" in d["text"] for d in result["discussion"] if d["round"] == 2)


def test_panel_review_synthesis_failure_keeps_votes(monkeypatch):
    fake = _fake_ask_factory([True]*4, [True]*4)
    def fake_with_bad_synth(prompt, **kw):
        if "what_happens" in prompt:
            return None
        return fake(prompt, **kw)
    monkeypatch.setattr(council, "_ask_json", fake_with_bad_synth)
    result = council.panel_review("ctx")
    assert result["approved"] is True
    assert result["summary"] == {}
