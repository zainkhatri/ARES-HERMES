#!/usr/bin/env python3
"""One-shot repair driver: re-request every video through the live /api/photos/hls
endpoint so the server (running the fixed pipeline) rebuilds any missing/corrupt/rotation-
flagged cache — transcoded baked-upright and stream-validated — and regenerates HLS.
Cooperates with the running prewarm via the server's per-key locks. Idempotent: good caches
with HLS already present return instantly.

Run:  ARES_PW=<pw> python3 rebuild_video_caches.py [concurrency]
"""
import os, sys, sqlite3, json, time, urllib.parse, urllib.request, http.cookiejar
from concurrent.futures import ThreadPoolExecutor

BASE = os.environ.get("ARES_BASE", "http://127.0.0.1:8090")
PW = os.environ.get("ARES_PW", "")
CONC = int(sys.argv[1]) if len(sys.argv) > 1 else 3

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def login():
    body = json.dumps({"password": PW}).encode()
    req = urllib.request.Request(BASE + "/api/login", data=body,
                                 headers={"Content-Type": "application/json"})
    with opener.open(req, timeout=15) as r:
        assert r.status == 200, f"login {r.status}"

def video_paths():
    con = sqlite3.connect("photo_index.db")
    out = []
    for path, item in con.execute("SELECT path,item FROM photos"):
        try:
            if json.loads(item).get("type") == "video":
                out.append(path)
        except Exception:
            pass
    return out

done = {"ok": 0, "err": 0, "n": 0}
t0 = time.time()

def rebuild(p):
    url = BASE + "/api/photos/hls?p=" + urllib.parse.quote(p, safe="")
    try:
        # Follow the 302; a cold transcode can take a while, so be patient.
        with opener.open(url, timeout=900) as r:
            ok = r.status in (200, 302) or str(r.url).endswith(".m3u8")
    except Exception:
        ok = False
    done["n"] += 1
    done["ok" if ok else "err"] += 1
    if done["n"] % 50 == 0:
        rate = done["n"] / max(time.time() - t0, 1)
        print(f"[rebuild] {done['n']}/{TOTAL} ok={done['ok']} err={done['err']} "
              f"{rate:.1f}/s", flush=True)
    return ok

if __name__ == "__main__":
    assert PW, "set ARES_PW"
    login()
    vids = video_paths()
    TOTAL = len(vids)
    print(f"[rebuild] {TOTAL} videos, concurrency {CONC}", flush=True)
    with ThreadPoolExecutor(max_workers=CONC) as ex:
        list(ex.map(rebuild, vids))
    print(f"[rebuild] COMPLETE ok={done['ok']} err={done['err']} "
          f"in {(time.time()-t0)/60:.1f} min", flush=True)
