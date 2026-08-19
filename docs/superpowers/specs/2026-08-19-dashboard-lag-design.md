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
