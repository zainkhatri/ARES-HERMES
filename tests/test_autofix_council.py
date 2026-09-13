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
