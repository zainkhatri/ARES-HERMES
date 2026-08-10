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
    assert picks[0]["ticker"] == "B" and picks[0]["rank"] == 1           # sorted by llm_score: B (100) > A (95) > C (25)
    assert picks[1]["ticker"] == "A" and picks[1]["rank"] == 2
    assert picks[2]["ticker"] == "C" and picks[2]["rank"] == 3
    print("  attach OK")

def test_sort_ranks_by_llm_score_alone():
    """Verify that data-fallback picks with high score outrank llama-hit picks with low score."""
    picks = [
        {"ticker": "LLAMA_LOW", "score": 50},   # will get llama score 40
        {"ticker": "DATA_HIGH", "score": 100},  # will get fallback score 100
        {"ticker": "FILLER", "score": 30},      # will get fallback score 30
    ]
    llm_map = {"LLAMA_LOW": {"score": 40, "reason": "weak signal"}}
    ep._attach_llm_scores(picks, llm_map)
    by = {p["ticker"]: p for p in picks}

    # Verify the scores were assigned correctly
    assert by["LLAMA_LOW"]["llm_score"] == 40, "llama hit should use llm score"
    assert by["DATA_HIGH"]["llm_score"] == 100, "data fallback max should be 100"
    assert by["FILLER"]["llm_score"] == 30, "data fallback should be normalized"

    # Verify the sort order: high-fallback beats low-llama
    assert picks[0]["ticker"] == "DATA_HIGH" and picks[0]["rank"] == 1, "DATA_HIGH (100) should rank #1"
    assert picks[1]["ticker"] == "LLAMA_LOW" and picks[1]["rank"] == 2, "LLAMA_LOW (40) should rank #2"
    assert picks[2]["ticker"] == "FILLER" and picks[2]["rank"] == 3, "FILLER (30) should rank #3"
    print("  sort order OK")

if __name__ == "__main__":
    test_llm_scores_parses_and_clamps()
    test_llm_scores_failure_returns_empty()
    test_attach_uses_llm_then_falls_back()
    test_sort_ranks_by_llm_score_alone()
    print("OK")
