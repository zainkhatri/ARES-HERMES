# Dashboard lag fix — ARES `:8080` & ZEUS `:8890`

**Date:** 2026-08-19
**Symptom:** Slow *initial load* of the ARES home dashboard and the ZEUS dashboard in Chrome on the Mac.

## Diagnosis (measured)

Ruled OUT, with evidence:
- **Server/render:** ARES `/` 22ms; ZEUS authenticated `/` 28ms. Both fast.
- **gzip:** both boxes already gzip `text/html` via the `_gzip_json` after_request hook (112KB → 29KB). *(Earlier "ZEUS ships uncompressed" was a false read off the 199-byte 302→/login response.)*
- **Network:** Mac connects direct on the LAN (192.168.20.x), not DERP-relayed.
- **Service worker:** navigation is network-first; the 6.7GB `PRECACHE_BUNDLE` kick was already removed.

**Root cause — render-blocking Google Fonts.** Every dashboard template loads
`fonts.googleapis.com/css2` (Bricolage Grotesque + JetBrains Mono) via a plain
`<link rel="stylesheet">` on the critical path. An external stylesheet blocks
first paint until it loads, regardless of `display=swap`. When the Mac's route/DNS
to Google is slow, the whole page hangs on it.

Headless proof (ARES home):

| Fonts condition | First Paint | DCL |
|---|---|---|
| reachable (fast) | 172ms | 208ms |
| **slow (+3s)** | **3168ms** | **3210ms** |
| blocked (fail fast) | ~100ms | 102ms |

Both dashboards share the template → both slow. Host probes were fast only
because the host's route to Google was fast at probe time.

## Fix (applied)

Make the font stylesheet non-render-blocking in all active templates:
```html
<link href="…fonts.googleapis.com/css2?…" rel="stylesheet"
      media="print" onload="this.media='all'">
```
Page paints immediately in a system fallback; the real font swaps in when it
arrives. Zero new files, no dependency, no server change.

- **ARES:** 9 templates (`_hud_head, index, login, business, files, home, drives, photos, dupes_review`), 10 links.
- **ZEUS:** same 9 templates in `/home/zain/ARES-DASHBOARD/templates`.
- No restart needed — `TEMPLATES_AUTO_RELOAD=True` on both.

**Result:** under slow-fonts, DCL 3210ms → 135ms. Render-block eliminated.

## Explicitly skipped (YAGNI — not the cause)
- flask-compress / gunicorn on ZEUS: gzip already present, server already fast.
- ZEUS `debug=True`/reloader: untidy but 28ms/request — not user-visible. Optional hygiene.
- ARES peer→ZEUS `/healthz` fetch timeout: a poll, doesn't block paint. Optional hardening.
- Self-hosting the fonts: would also remove the swap delay, but the async load
  already fixes the reported problem. Revisit only if the font swap-in is bothersome.

---

## Part 2 — runtime freeze (2026-08-19): it was a Chrome GPU blocklist, NOT the code

**Symptom:** With any dashboard tab open, Chrome lagged out the whole MacBook —
*instant and constant*, on **both** ARES and ZEUS (which share the template).

**Wrong turns (recorded so nobody repeats them):**
- Guessed the full-HUD `zoom` fit-scaling was a retina re-raster cost. Removed it.
  **Did not help.** Reverted — with GPU compositing healthy, `zoom` is cheap and is
  the intended single-screen design.
- Profiled the rendered page headless (GPU-accelerated Linux Chrome): idle = **0**
  rAF / **0** long tasks / 0 CSS animations; the 3s update tick = **5–6ms** heavy,
  **<1ms** light, **0** long tasks, 951 DOM nodes. The frontend is objectively fast.
  This *ruled out* the code and forced measurement on the real hardware.

**Actual root cause — Chrome fell back to SOFTWARE compositing.** On the user's
machine `chrome://gpu` showed Compositing/Rasterization "Software only." Cause: a
brand-new **Apple M5 on macOS 26.5.2** was not yet in Chrome 151's GPU safelist, so
Chrome blocklisted the GPU and did every retina (dpr 2) repaint on the CPU. That is
what froze the whole machine, on every page, regardless of how light the DOM is.

**Fix (client-side, zero code):** `chrome://flags/#ignore-gpu-blocklist` → Enabled →
relaunch. `chrome://gpu` then reports Compositing + Rasterization + Canvas + WebGL +
WebGPU all **Hardware accelerated** (Metal, ANGLE Metal Renderer, Apple M5). Durable
across restarts. Can be dropped once a Chrome update adds M5/macOS 26 to the safelist.

**Diagnostic that nailed it** (paste in Console on the laggy page, watch 5s):
```js
(()=>{let f=0,lt=0,t0=performance.now();
try{new PerformanceObserver(l=>{for(const e of l.getEntries())lt+=e.duration}).observe({entryTypes:['longtask']})}catch(e){}
(function loop(){f++;requestAnimationFrame(loop)})();
setTimeout(()=>console.log(`FPS ~${Math.round(f/((performance.now()-t0)/1000))} | blocked ${Math.round(lt)}ms | dpr ${devicePixelRatio}`),5000)})();
```
Plus `chrome://gpu` top block. If it says "Software only" for Compositing/Raster on a
box that has a real GPU → it's a blocklist, not the site.

**Lesson:** "lightweight page freezes the whole machine, identically on two unrelated
servers" ⇒ suspect the client's GPU pipeline, not the app. Measure before editing.

**Aside:** the remaining "Video Decode: Software only" is from an explicit
`--disable-accelerated-video-decode` flag on the Mac — unrelated to HUD lag. Remove it
if HW video decode is wanted (photos gallery / VM stream).
