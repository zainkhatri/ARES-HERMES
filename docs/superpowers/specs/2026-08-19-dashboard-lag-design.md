# Dashboard lag fix — ARES `:8080` & ZEUS `:8890`

**Date:** 2026-08-19
**Goal:** Both dashboards load smoothly in Chrome on the Mac. User reports *slow initial load* on the ARES home dashboard and the ZEUS dashboard.

## Diagnosis (measured, not guessed)

| Check | ARES `:8080` | ZEUS `:8890` |
|---|---|---|
| Server | gunicorn behind Caddy | **`python app.py` — Werkzeug dev server** |
| Debug mode | off | **`debug=True` + reloader ON** (no `--no-debug` in live cmdline) |
| gzip | yes (Caddy) — 112KB → 29KB | **none — ships full 112KB uncompressed** |
| `/` server render | 22ms | slow (debug overhead) |
| Network to Mac | direct LAN (192.168.20.x), not DERP-relayed | direct LAN |
| Headless full load | 298ms, 895 DOM nodes, one 60ms long task | n/a |
| Service worker | network-first on HTML; not registered on home | n/a |

**Conclusions:**
1. **ZEUS is the real bottleneck.** Werkzeug in debug mode + no gzip = slow initial load and 4x transfer size.
2. **ARES home is fast in isolation.** Its perceived slowness is a *downstream symptom*: the home page populates its "sister box" widget with a **browser-side cross-box fetch to ZEUS `/healthz`** (in `pollPeer`, `home.html`), with **no timeout**. When ZEUS's debug server lags, that widget hangs and the whole page feels slow to settle.
3. Bonus: ZEUS runs a second, older dashboard (`zeus-dash.service` → `/srv/.../ZEUS-DASH`, ~`:8888`) also in debug mode — extra reloader file-watching load on the box. Retiring it is optional cleanup.

## Changes

### ZEUS `:8890` (core fix)
1. `flask-compress` into ZEUS venv: `/home/zain/ARES-DASHBOARD/.venv/bin/pip install flask-compress`.
2. In ZEUS's `app.py`, add a guarded compression block near app creation:
   ```python
   if os.getenv("ARES_GZIP") == "1":
       try:
           from flask_compress import Compress
           Compress(app)
       except Exception:
           pass  # gzip is a nicety, never fatal
   ```
   Guarded so it's a no-op on ARES (already gzipped by Caddy) — no double-compression.
3. Edit `zeus-dashboard.service` (user unit): add `--no-debug` to `ExecStart` and `Environment=ARES_GZIP=1`.
4. `systemctl --user restart zeus-dashboard` (as `zain`).

### ARES home (harden against slow peer)
5. In `templates/home.html`, `pollPeer()`: add an `AbortController` timeout (~2.5s) to the cross-box `/healthz` fetch so a slow/down ZEUS never leaves the sister widget hanging — it fails fast and renders "down/unknown" instead of blocking.
6. Add the same guarded `Compress(app)` block to the host `app.py` (harmless; gated off by default).

### Explicitly skipped (YAGNI)
- Static `Cache-Control`: `home.html` is fully self-contained (inline CSS/JS, no external assets) — near-zero benefit.
- Poll-stagger: the load-time polls are async and lightweight; no measured benefit once ZEUS is fast.
- gunicorn on ZEUS: `threaded=True` already covers single-user concurrency; extra dep + wiring for no gain.
- Retiring `zeus-dash.service`: optional, out of core scope.

## Verification
- `curl -sD- http://100.100.29.36:8890/ -H 'Accept-Encoding: gzip'` → `Content-Encoding: gzip`, size ~29KB (was ~112KB).
- Confirm live cmdline has `--no-debug` and no reloader child process.
- Re-measure ARES home real-browser load (browser bridge / headless): sister-box widget settles within the 2.5s cap even if ZEUS is stopped.
