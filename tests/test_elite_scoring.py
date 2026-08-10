import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import elite_picks as ep

class _FakeResp:
    def __init__(self, code, payload): self.status_code = code; self._p = payload
    def json(self): return self._p

def test_llm_scores_parses_and_clamps():
    payload = {"response": json.dumps({"scores": [
        {"ticker": "AMZN", "score": 150, "reason": "many elites, strong momentum"},
        {"ticker": "v", "score": "88", "reason": "legends only"},
        {"ticker": "BAD", "score": None, "reason": "x"},        # dropped: bad score
        {"ticker": "NORE", "score": 50, "reason": ""},           # dropped: empty reason
    ]})}
    ep.requests = type("R", (), {"post": staticmethod(lambda *a, **k: _FakeResp(200, payload))})
    out = ep._llm_scores([{"ticker": "AMZN", "name": "Amazon", "buyers": 9, "firms": [], "momentum": None, "upside": None}])
    assert out["AMZN"]["score"] == 100, out          # clamped 150->100
    assert out["V"]["score"] == 88                    # upper-cased + coerced
    assert "BAD" not in out and "NORE" not in out
    print("  llm_scores OK")

def test_llm_scores_failure_returns_empty():
    def boom(*a, **k): raise RuntimeError("ollama down")
    ep.requests = type("R", (), {"post": staticmethod(boom)})
    assert ep._llm_scores([{"ticker": "X", "name": "X", "buyers": 1, "firms": []}]) == {}
    print("  llm_scores failure OK")

def test_attach_uses_llm_then_falls_back():
    picks = [
        {"ticker": "A", "score": 10}, {"ticker": "B", "score": 20}, {"ticker": "C", "score": 5},
    ]
    ep._attach_llm_scores(picks, {"A": {"score": 95, "reason": "r"}})
    by = {p["ticker"]: p for p in picks}
    assert by["A"]["llm_score"] == 95 and by["A"]["llm_reason"] == "r"   # llama value
    assert by["B"]["llm_score"] == 100                                   # fallback: 20 is max -> 100
    assert by["C"]["llm_score"] == 25                                    # fallback: 5/20*100
    assert picks[0]["ticker"] == "A" and picks[0]["rank"] == 1           # sorted by llm_score
    print("  attach OK")

if __name__ == "__main__":
    test_llm_scores_parses_and_clamps()
    test_llm_scores_failure_returns_empty()
    test_attach_uses_llm_then_falls_back()
    print("OK")
