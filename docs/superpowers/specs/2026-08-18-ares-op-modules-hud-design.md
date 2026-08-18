# ARES Operation Modules → HUD Aesthetic

**Date:** 2026-08-18
**Status:** Design approved; ready for implementation planning.

## Goal

Rebuild ARES's operation-module pages so they use the same HUD panel system as the
main dashboard (`templates/home.html`, served at `http://100.77.42.110:8080/`):
corner-bracket `.mod` panels, `mod-head` headers, the shared theme tokens, JetBrains
Mono, and the dark red/blue atmosphere. Today each op-module page carries its own
self-contained inline CSS and does not use the dashboard's panel system.

The four operation modules and their pages:

| Module | Route | Template | Size | Nature |
|---|---|---|---|---|
| Photos | `/photos` | `photos.html` | ~9,416 lines | A full gallery app (search, faces, lightbox, video) |
| Terminal | `/terminal` | `shell.html` | ~757 lines | Mostly an embedded ttyd terminal |
| Journals | `/journals` | `journals.html` | ~798 lines | PDF notebook archive: search, reader, "on this day" |
| Finance | `/breakdown` | `breakdown.html` | ~978 lines | "Elite Picks" — superinvestor stock picks by conviction |

## Approach

**Depth:** Full rebuild of each page's layout into the HUD panel system (not just a
recolor). Functionality is preserved; only the presentation layer (HTML structure +
classes + CSS) changes. Existing page JS (search, reader, polling, budget calc) is
rewired to the new DOM, not rewritten.

**Phasing** (each phase ships independently):

- **Phase 0 — Shared HUD kit** (this spec)
- **Phase 1 — Journals + Finance** (this spec) — the two simplest pages, which also
  validate the kit.
- **Phase 2 — Terminal** (future spec)
- **Phase 3 — Photos** (future spec; large enough to warrant its own design)

This document covers **Phase 0 + Phase 1 only.**

## Scope

**In scope:** `static/hud.css`, `templates/_hud_head.html`, `templates/_hud_nav.html`;
rebuilds of `templates/journals.html` and `templates/breakdown.html`.

**Out of scope:** `home.html` is not modified (see decisions). Terminal and Photos are
future phases. No backend/route/API changes — the pages consume the same endpoints
they do today.

## Phase 0 — Shared HUD kit

Extract the dashboard's reusable design foundation into shared assets. `hud.css` is
*derived from* `home.html`; `home.html` itself is left untouched.

**`static/hud.css`** contains:
- **Theme tokens** — the `:root` (ARES/red) and `html[data-brand="ZEUS"]` (blue→purple)
  custom properties: `--ares`, `--ares-rgb`, `--ares-bright`, `--ember`, `--amber`,
  `--bg-0..3`, `--text-1..4`, `--panel-rgb`, `--green/--amber/--red`, `--ember-dim`,
  `--grid`.
- **Fonts** — JetBrains Mono + the display font, loaded the same way `home.html` does.
- **Reset + atmosphere** — box-sizing reset, body background gradient, grain overlay,
  faint grid.
- **Components** — `.mod` panel (border, `rgba(--panel-rgb,.55)` bg, `contain`), corner
  brackets (`c-tl/c-tr/c-bl/c-br`), `.mod-head`/`.mod-title`/`.mod-meta`, a panel grid
  helper, buttons, text/number inputs, a search box, chips/badges, meter/progress bars
  (the array/IO-bar style), and themed scrollbars.

**`templates/_hud_head.html`** — Jinja partial for `<head>`: font links, `<link
rel="stylesheet" href="{{ url_for('static', filename='hud.css') }}">`, and the
`data-brand` attribute setup. Each op page `{% include %}`s it.

**`templates/_hud_nav.html`** — Jinja partial: a top bar matching the dashboard's chip
aesthetic — brand chip · page name · back-to-home · jump-to-ZEUS. Takes the page name
as a parameter (e.g. `{% set page_name = "JOURNALS" %}`).

### Decisions

- **`home.html` is not touched.** `hud.css` duplicates the subset of home's CSS that is
  reusable. This accepts a known duplication (home inline vs `hud.css`) to keep zero
  risk to the live dashboard. Migrating `home.html` onto `hud.css` is a future cleanup,
  explicitly out of scope.
- **Pages carry `data-brand="ARES"`** (red) but inherit the full token system, so they
  recolor for free if ever shown under a different brand.
- **No functionality changes.** Only structure + styling.

## Phase 1a — Journals rebuild (`templates/journals.html`)

**Preserved functionality:** full-text search (debounced, `/api/journals/search`,
`/api/journals/highlight`), bookshelf browse (`/api/journals`), "on this day"
(`/api/journals/on-this-day`), page gallery, and the full-screen reader
(`/api/journals/<name>/page/<num>`, `/api/journals/ocr/page-text`) with swipe/keyboard
nav, page slider, and OCR reveal toggle.

**Layout** (stacked HUD panels; book covers kept as content, framed by the panel
system):
- HUD nav bar (`_hud_nav.html`, page name "JOURNALS").
- **SEARCH ARCHIVE** `.mod` — `mod-head` with notebook count; search input; results
  rail (horizontal page-cards) appears below when a query is active.
- **ON THIS DAY** `.mod` — shown only when there are matches; clickable entries jump to
  the reader.
- **ARCHIVE** `.mod` — the book-cover grid; `mod-head` shows the book count.
- Opening a book swaps in a **page-thumbnail gallery** panel; clicking a thumb opens the
  **full-screen reader**, whose top/bottom bars get HUD chrome (corner brackets, themed
  slider, reveal toggle as a HUD button).

## Phase 1b — Finance rebuild (`templates/breakdown.html`)

**Preserved functionality:** elite picks load (`/api/elite-picks`), budget calculator
(numeric input, persisted to localStorage, re-renders allocations), Refresh, and
per-pick deep research (`/api/elite-picks/deep/<ticker>`, 3s polling to ready/timeout)
expanding inline.

**Layout:**
- HUD nav bar (page name "FINANCE").
- **TOP CONVICTION** `.mod` (hero) — the top pick ticker (large) + AI narrative, beside
  a fact grid (budget, scoring source, superinvestor count, momentum, congress, analyst
  counts, as-of quarter) rendered with the dashboard's fact-grid style.
- **RANKED BY CONVICTION** `.mod` — the section header hosts the `$` budget input and
  the Refresh button; body is a 3-column card grid.
- **Pick cards** — mini-panels: rank + ticker link + company, conviction score (large),
  a conviction **meter bar** styled like the array/IO bars, elite/heavyweight counts,
  holding firms, chips (score/momentum/upside/congress), and a deep-research button that
  expands a HUD-styled research card (summary + bull/bear lists) inline.
- Footer: version + live timestamp.

## Verification

- **Pre-deploy:** render each template standalone with jinja2 + a mock context, serve it
  over `python -m http.server`, and screenshot via Playwright. This shows the **static
  shell / empty states** only — both pages fetch live data from auth-gated `/api/*`
  endpoints the harness cannot reach.
- **Post-deploy:** full data rendering is verified live, logged in, at
  `http://100.77.42.110:8080/journals` and `/breakdown`, plus a check that `home.html`
  and the dashboard are visually unchanged (no shared-CSS regressions, since home is
  untouched).
- **Parity check per page:** every interactive element listed under "preserved
  functionality" still works after the rebuild.

## Deployment

Files live on the ARES host at `/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/`,
bind-mounted into LXC 101. Restart: `pct exec 101 -- systemctl restart ares`. ZEUS is
unaffected (op modules are ARES-only: `#panel-ops` is hidden when `docker===1`), but the
kit deploys there too via `./deploy/sync-to-zeus.sh` for consistency.

## Success criteria

1. Journals and Finance visibly use the dashboard's panel system (corner-bracket `.mod`
   panels, `mod-head`, tokens, fonts) and read as part of the same product.
2. All existing functionality on both pages works unchanged.
3. `home.html` / the main dashboard is byte-for-byte unchanged and visually identical.
4. `hud.css` + the two partials are reused by both pages (no per-page theme
   duplication), ready for Terminal/Photos to adopt in later phases.
