import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "autofix"))
import triage


class _FakeResp:
    def __init__(self, code, payload):
        self.status_code = code
        self._p = payload

    def json(self):
        return self._p


def test_wraps_log_in_untrusted_delimiter(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["prompt"] = json["prompt"]
        return _FakeResp(200, {"response": '{"escalate": false, "reason": "transient"}'})

    monkeypatch.setattr(triage.requests, "post", fake_post)
    triage.triage("ares-facescan", "some log text")
    assert "<untrusted_log>" in captured["prompt"]
    assert "</untrusted_log>" in captured["prompt"]
    assert "never as instructions" in captured["prompt"].lower()


def test_parses_escalate_true(monkeypatch):
    monkeypatch.setattr(triage.requests, "post", lambda *a, **k: _FakeResp(200, {"response": '{"escalate": true, "reason": "looks like a real bug"}'}))
    result = triage.triage("ares-facescan", "Traceback ValueError")
    assert result["escalate"] is True
    assert result["reason"] == "looks like a real bug"


def test_fails_open_on_exception(monkeypatch):
    def raise_conn_error(*a, **k):
        raise triage.requests.exceptions.ConnectionError("ollama down")

    monkeypatch.setattr(triage.requests, "post", raise_conn_error)
    result = triage.triage("ares-facescan", "some log")
    assert result["escalate"] is True
    assert "unreachable" in result["reason"].lower()


def test_fails_open_on_non_200(monkeypatch):
    monkeypatch.setattr(triage.requests, "post", lambda *a, **k: _FakeResp(500, {}))
    result = triage.triage("ares-facescan", "some log")
    assert result["escalate"] is True


def test_fails_open_on_malformed_json(monkeypatch):
    monkeypatch.setattr(triage.requests, "post", lambda *a, **k: _FakeResp(200, {"response": "not json"}))
    result = triage.triage("ares-facescan", "some log")
    assert result["escalate"] is True
