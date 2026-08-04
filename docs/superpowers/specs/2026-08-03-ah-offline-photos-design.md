# A&H App — Offline Photo Caching

**Date:** 2026-08-03  
**Scope:** iOS app (PROMETHEON Xcode project) + Flask backend  
**Goal:** Photos viewed while on tailnet remain accessible when offline. System manages cache size and eviction — no user config.

---

## Background

The A&H app is a WKWebView wrapper around the ARES dashboard (`ares.tail3045df.ts.net`). Photo thumbnails are served by Caddy directly from `static/thumbs*/` at four tiers (475px, 800px, 2048px, 2560px). When the tailnet is unreachable, navigation fails and all photos disappear.

The app is served over HTTPS (valid Tailscale cert), which is required for service workers in WKWebView (iOS 16+). This makes a service worker the right caching primitive: it runs in the web layer, requires no Swift caching logic, and hands eviction to the browser's Cache API storage (system-managed, no cap to set).

---

## What Changes

### 1. Service Worker (`static/sw.js`)

A new file at `/static/sw.js`, served by Flask with two headers:
- `Cache-Control: no-cache` — so updates deploy immediately
- `Service-Worker-Allowed: /` — required because the file lives under `/static/` but claims root scope; without this the browser silently rejects registration

**Install:** pre-caches the app shell — `photos.html`, `home.html`, and core JS/CSS bundles. These are the files needed to render the page skeleton offline.

**Fetch strategy — stale-while-revalidate for photos:**
- Intercepts requests matching `/static/thumbs/*`, `/static/thumbs_hq/*`, `/static/thumbs_preview/*`, `/static/thumbs_max/*`
- On hit: return cached response immediately, revalidate in background
- On miss + online: fetch, cache, return
- On miss + offline: return a transparent 1×1 placeholder (no broken-image icon)

**Fetch strategy — network-first for API:**
- `/api/photos/all-months`, `/api/photos/items` — try network, fall back to cache
- Everything else — network only (no caching for auth/vault/shell routes)

**Cache name versioned:** `ah-photos-v1`. On `activate`, old cache versions are deleted.

### 2. SW Registration (`templates/photos.html` + `templates/home.html`)

Add at the bottom of `<body>` in both files:

```html
<script>
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/static/sw.js', { scope: '/' });
}
</script>
```

No await, no error surfacing — registration is fire-and-forget. If the browser doesn't support it (old iOS), photos just work online as before.

### 3. Flask: Cache-Control headers on photo endpoints

The thumb-serving routes in `app.py` currently return default headers. Add:

```
Cache-Control: public, max-age=604800, immutable
```

(7 days, immutable — thumb filenames are content-derived hashes, so they never change in place.)

This covers:
- The Flask cold-path thumb-serving routes (`/static/thumbs/<path>`, etc.)
- The `/api/photos/download` route already bypasses cache (that's fine)

Caddy serves thumbs directly for warm paths — add a matching header in `Caddyfile` for the `static/thumbs*` file_server blocks.

### 4. Swift: App rename PROMETHEON → A&H

While touching the project, fix the strings that still say PROMETHEON:

- `Info.plist`: `CFBundleDisplayName` → `A&H`; both `NSPhotoLibraryAddUsageDescription` / `NSPhotoLibraryUsageDescription` strings updated
- `App.swift`: `Text("PROMETHEON")` → `Text("A&H")`; rename `PROMETHEONApp` → `AHApp`; rename `BlockP` → `BlockAH` and rewrite its shape to match the new pixel-art icon (two stems with a crossbar, not a P)
- `ContentView.swift`: `Text("PROMETHEON")` → `Text("A&H")`; `BlockP(...)` → `BlockAH(...)`

No functional changes — purely cosmetic rename.

---

## Offline UX

| State | Behavior |
|---|---|
| Online, photo not yet viewed | Loads from server, SW caches it |
| Online, photo previously viewed | SW returns cached copy immediately, revalidates in background |
| Offline, photo previously viewed | SW returns cached copy |
| Offline, photo never viewed | SW returns transparent 1×1 placeholder; no broken-image icon |
| Offline, page itself | SW serves cached app shell; grid renders with cached/placeholder mix |

No loading states, no "you are offline" banners — the page just works as far as it can. The user will notice uncached photos are blank; that's acceptable.

---

## What's Not In Scope

- **Pinning / explicit favorites** — not needed; system evicts least-recently-used automatically
- **Background prefetch** — not added; cache warms naturally as you browse
- **Push notifications** — separate feature
- **Video offline** — HLS streams are not cached; too large, complex chunked format

---

## File Changelist

| File | Change |
|---|---|
| `static/sw.js` | New — service worker |
| `templates/photos.html` | Add SW registration snippet |
| `templates/home.html` | Add SW registration snippet |
| `app.py` | Add `Cache-Control: public, max-age=604800, immutable` to thumb routes |
| `Caddyfile` | Add matching header block for `static/thumbs*` file_server |
| `ios/PROMETHEON/Info.plist` | Bundle display name + usage strings → A&H |
| `ios/PROMETHEON/App.swift` | Rename app struct, BlockP → BlockAH, strings |
| `ios/PROMETHEON/ContentView.swift` | Strings + BlockP → BlockAH |
