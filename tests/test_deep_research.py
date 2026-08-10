import os, sys, json, time, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app

def test_cache_paths_sanitize():
    j, m = app._deep_cache_paths("brk.b", "Q1 2026")
    assert os.path.basename(j) == "BRK.B_Q12026.json", j
    assert m.endswith(".running")
    # path traversal is stripped
    j2, _ = app._deep_cache_paths("../../etc/pw", "Q1 2026")
    assert "/etc/" not in j2 and os.path.basename(j2).endswith(".json")
    print("  paths OK")

def test_lookup_states(tmpdir_monkeypatch=None):
    d = tempfile.mkdtemp()
    app._DEEP_DIR = d
    assert app._deep_lookup("AAA", "Q1") is None                     # nothing yet
    j, m = app._deep_cache_paths("AAA", "Q1")
    open(m, "w").write(str(time.time()))
    assert app._deep_lookup("AAA", "Q1")["status"] == "running"      # fresh marker
    open(m, "w").write("0")                                          # stale marker
    assert app._deep_lookup("AAA", "Q1") is None
    json.dump({"summary": "x", "bull": [], "bear": [], "used_web": False}, open(j, "w"))
    r = app._deep_lookup("AAA", "Q1")
    assert r["status"] == "ready" and r["brief"]["summary"] == "x"   # cache wins
    print("  lookup OK")

if __name__ == "__main__":
    test_cache_paths_sanitize()
    test_lookup_states()
    print("OK")
