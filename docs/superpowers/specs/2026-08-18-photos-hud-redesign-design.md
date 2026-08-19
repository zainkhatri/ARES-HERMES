# ARES Photos → Full-HUD Redesign

**Date:** 2026-08-18
**Status:** Design approved; ready for implementation planning (Phase 0 first).

## Goal

Make ARES Photos (`templates/photos.html`, served at `/photos`) as UI/UX-friendly as
possible across desktop and iPhone, by (a) bringing it onto the shared HUD design system
the other dashboard modules now use, and (b) fixing the worst UX friction — flow by flow.
"Reskin + targeted UX," delivered in independently-shippable phases, **without breaking the
daily-driver flows or compromising the My-Eyes-Only vault.**

## What Photos is today

One 9,474-line Jinja template of hand HTML/CSS/vanilla JS — no build step. Deploy is: edit
the file → `pct exec 101 -- systemctl restart ares` → live. It is a Google-Photos-class
gallery for ~45,000 personal photos, used daily by the owner on both desktop and iPhone
(installed as a PWA). Feature surfaces:

- **Browse** — month-grid gallery (`#flat-grid-main`) + a finger-tracking year/month
  fast-scroll scrubber (`#fs-years`, `#fs-label-year/month`).
- **Modes** — Photos / Videos / Screenshots / People (chrome buttons + `#mobile-tabbar`).
- **Find** — CLIP semantic search (`#chrome-search-input`, `#search-overlay`,
  `#search-filters`, `#search-grid`, `#inline-search-grid`) and People/faces
  (`#people-panel`, `#people-grid`, name-faces `#nf-overlay`, per-person
  `#person-photos-grid`).
- **View** — full-screen lightbox (`#lb-month`, pinch zoom, full-res tier, EXIF
  `#lb-exif-panel`, HLS video, swipe paging).
- **Select** — multi-select bar (`#sel-bar`) → move to vault (`#sel-vault`).
- **Vault** — "My Eyes Only": PIN (`#pin-overlay`) + WebAuthn/FaceID biometric
  (`#vault-overlay`, `#vault-grid`, `#vault-biometric-*`). Server is the source of truth;
  plaintext must never persist; vault items are excluded from all listing endpoints via
  `_get_vault_hashes()`.

## Design direction — Full HUD

The owner chose **Full HUD** (over "content-first" or "photos-first, accent-only"): the HUD
language — corner-bracket panels, JetBrains Mono labels, `#f5402d` red accent, warm-black
background — is applied **throughout**, including the grid, for maximum cohesion with the
rest of the dashboard. This is deliberate and overrides the council's Outsider caution that
monospace/brackets can fight photo content; the mitigation is *restraint in density*, not a
different aesthetic:

- Photo **thumbnails are never tinted or bracketed at rest** — the image is shown clean.
  The HUD accents (a 9px corner tick + a red mono date) appear **on hover/focus only**, so
  the brackets read as "HUD" without turning the grid into visual noise.
- Inside the **lightbox**, the frame is near-black so the photo is the hero; HUD chrome
  lives in the top bar, the EXIF panel, and the swipe indicator — not over the image.
- Everywhere else (bars, mode tabs, month headers, panels, empty states, search, people,
  vault) is unrestrained Full HUD.

A visual mock of the Browse surface (chrome + month grid + scrubber + lightbox-with-EXIF)
was built with the real `hud.css` and approved. Reproduce it from this spec; it is not
committed.

## Architecture — reskin once, then phase the UX

The council's load-bearing insight: a **phased reskin of a 9,474-line monolith is a
five-phase rewrite tax with no working software in between** — every half-converted phase
leaves two visual languages colliding. Therefore:

1. **The visual reskin is ONE cut (Phase 0), CSS-only, scoped, and revertable.** Wrap the
   page in `<body class="photos-page">` and prefix every new HUD rule with `.photos-page`.
   Add the `hud.css` `:root` tokens (or `@import`/`<link>` it) so the existing inline CSS
   can consume the token variables. **No JavaScript and no DOM structure changes in Phase
   0.** If anything regresses, reverting is removing one block / one class.
2. **The per-surface UX improvements are phased AFTER the reskin** (Phases 1–4). Each phase
   touches one bounded surface, ships independently, and is verified on iPhone before the
   next begins.
3. **No backend changes** are required by this design. The page consumes the same
   auth-gated `/api/photos/*` endpoints it does today. (Any perf work in Phase 1 that would
   need a new endpoint is called out there and, if adopted, gets its own spec.)

### Global constraints (bind every phase)

- **Vault is a security kill-zone, not a skin.** Never edit vault logic in the same commit
  as reskin/UX work. A CSS refactor that lets a vault thumbnail enter the grid DOM or repaint
  — even for one frame — is a failure. Vault items must remain excluded via the existing
  hash-set gates; verify the vault-exclusion path is untouched after every phase.
- **Scope all new CSS under `.photos-page`.** Do not add or modify global/unscoped selectors;
  `hud.css` tokens may be referenced but the shared component rules must not be edited from
  here. `home.html` and the other modules must remain byte-for-byte visually unchanged.
- **Daily-driver safety.** Back up before every editing session
  (`cp templates/photos.html templates/photos.html.$(date +%Y%m%d-%H%M).bak`). After any
  change to the scrubber or the lightbox, smoke-test on a real iPhone (finger-tracking and
  touch paging are fragile and the render harness cannot exercise them).
- **No new dependencies, no build step.** Hand HTML/CSS/JS only, consistent with the page.
- **Preserve every listed behavior.** Modes, search, faces/naming, per-person, multi-select,
  vault move, lightbox zoom/EXIF/HLS/swipe, PWA install — all keep working.
- **Commit hygiene:** subject line only, no body, no Co-Authored-By trailer.

## Phase plan

Each phase = reskin-consistent + one headline UX win. Phases 1–4 are outlined here; each
gets its **own** spec + implementation plan when reached. Phase 0 is detailed below.

| Phase | Surface | Reskin | Headline UX win |
|---|---|---|---|
| **0** | Whole page (chrome/frame) | Scope `.photos-page`, apply HUD tokens + chrome/panel styling — **CSS-only** | Consistent HUD look, **zero functional risk** |
| **1** | Browse (grid + month heads + scrubber) | HUD tiles, month dividers, scrubber rail | **First-paint** (current month renders before full payload) + **scroll-restore on lightbox close** |
| **2** | Find (search + people/faces) | HUD search bar, filters, people grid | **Search-first**: one prominent bar parsing CLIP + name + date; discoverable, not buried |
| **3** | View (lightbox) | Near-black frame, HUD top bar + EXIF panel | Zoom/swipe polish + **EXIF-as-story** (camera/lens/GPS/golden-hour) |
| **4** | Vault + Videos/Screenshots modes | HUD chrome for vault + filtered modes | Vault stays **isolated + last**; never enters grid DOM |

## Phase 0 — Foundation reskin (this spec's implementation target)

**Scope:** `templates/photos.html` only. **CSS + two markup touches** (the `body` class and
the token bring-in). **No JS, no DOM restructure, no behavior change.**

**In scope:**
- Add `class="photos-page"` to `<body>` (the single scoping hook).
- Bring the HUD tokens into the page: add `<link rel="stylesheet" href="{{ url_for('static',
  filename='hud.css') }}">` in `<head>`, loaded **before** the existing inline `<style>` so
  the page's own rules win on specificity and can consume `--ares`, `--bg-0..3`,
  `--text-1..4`, `--ember-dim`, etc. Verify no `hud.css` global (`* {}` reset, `body`
  background) fights the existing layout; if it does, neutralize it **scoped under
  `.photos-page`**, never by editing `hud.css`.
- Restyle the **chrome only**, scoped under `.photos-page`, to the mock:
  - top bar: brand chip + mode tabs (`.px-mode`, active = red glow) + prominent search
    field + vault button;
  - month headers → HUD dividers (Bricolage title + red year + mono count);
  - panels (favorites/screenshots/memories/people/search/vault overlays) → `.mod` framing
    with corner brackets and `.mod-head`;
  - buttons/inputs/chips → HUD `.btn`/`.field` token styling;
  - empty/loading states → HUD treatment (tighten the cavernous ones, as done for journals).
- **CRITICAL — must NOT change in Phase 0:** thumbnail markup, grid JS, scrubber JS/geometry,
  lightbox, search JS, faces JS, vault JS, selection JS, any element ID or class the JS reads,
  and any hardcoded pixel value the JS computes against. Thumbnails render clean; HUD tile
  accents (corner tick + hover date) are additive CSS on the existing tile element only.

**Out of scope (Phase 0):** grid virtualization/first-paint (Phase 1), scrubber behavior
(Phase 1), search flow (Phase 2), lightbox internals (Phase 3), vault internals (Phase 4),
any `.py` change.

**Verification (Phase 0):**
- *Pre-deploy:* render `photos.html` standalone (jinja mock, empty data) → `python -m
  http.server` → Playwright screenshot the **chrome shell** at desktop (1280) and mobile
  (390) widths. Confirms tokens/chrome only; the harness cannot load the 45k-item data or
  exercise touch.
- *Post-deploy (human, logged in over Tailscale):* on desktop and iPhone —
  1. grid still scrolls and lazy-loads; 2. the fast-scroll scrubber still finger-tracks and
  jumps; 3. tapping a photo still opens the lightbox; zoom/swipe/EXIF/video still work;
  4. search still returns results; 5. People/faces + naming still work; 6. multi-select →
  vault still works; 7. **vault still unlocks (PIN + FaceID) and no vault item ever appears
  in any grid**; 8. PWA still installs; 9. `home.html` and the other modules are visually
  unchanged.
- *Regression tripwire for the vault:* after the reskin, confirm `_get_vault_hashes()` /
  the vault-exclusion filter is still applied on every listing path and that a known vault
  item does not render in Photos, Videos, Screenshots, People, or Search.

**Deployment:** back up the file first; edit on the ARES host (bind-mounted into LXC 101);
`pct exec 101 -- systemctl restart ares`; verify `/photos` 200/302; then
`./deploy/sync-to-zeus.sh --restart` for parity (op modules are ARES-only in behavior but
the template deploys to both).

## Phases 1–4 — outlines (each gets its own spec later)

- **Phase 1 — Browse.** HUD grid tiles (clean at rest, hover = corner tick + red mono date),
  HUD month dividers, HUD scrubber rail with red active dot + finger-tracking bubble. UX:
  **first-paint** — render the current/most-recent month immediately and hydrate the rest
  progressively instead of blocking on the full `all-months` decode; and **scroll-position
  restoration** so closing the lightbox returns to the exact grid offset (the single most
  common gallery annoyance). iPhone smoke test mandatory.
- **Phase 2 — Find.** Promote search to a first-class, always-visible entry point; one input
  that routes to CLIP semantic search, a person name, or a date, with the filter set shown as
  HUD chips. Reskin the People grid, name-faces overlay, and per-person view; make "name this
  face" fast and obvious.
- **Phase 3 — View.** Lightbox: near-black frame, smooth pinch-zoom and swipe paging (fix any
  stutter / wrong-anchor close), full-res tier on zoom, and the **EXIF-as-story** panel
  (camera, lens, exposure, GPS/place, golden-hour flag) rendered in HUD. Ensure HLS video
  auth (cookie/header) works for segments.
- **Phase 4 — Vault + secondary modes.** Reskin the Videos and Screenshots filtered modes.
  Reskin the vault (PIN, biometric enroll, grid) as an **isolated** surface — no persistent
  plaintext, purge + cancel on lock, server lock is source of truth. Done **last**, in its
  own commits, never mixed with other work.

## Success criteria

1. Photos visibly uses the HUD design system (bracket panels, mono labels, red accent,
   warm-black) across chrome and grid, and reads as part of the same product as the
   dashboard.
2. Every existing behavior (all modes, search, faces/naming/per-person, multi-select, vault
   move, lightbox zoom/EXIF/HLS/swipe, PWA) still works on desktop and iPhone.
3. The vault is uncompromised: no vault item ever appears in any grid/search, and vault
   logic was never edited alongside reskin/UX work.
4. `home.html` and the other modules are visually unchanged (no shared-CSS regression; all
   new CSS is scoped under `.photos-page`).
5. Phase 0 ships as a CSS-only, revertable cut; Phases 1–4 each ship independently with their
   headline UX win, verified on a real iPhone.
