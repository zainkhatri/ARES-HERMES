# ARES Op-Modules HUD Rebuild — Implementation Plan (Phase 0 + Phase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give ARES's Journals and Finance pages the dashboard's HUD look by building a shared CSS/partials kit and rebuilding both pages onto it.

**Architecture:** Extract the dashboard's reusable design foundation (tokens, atmosphere, `.mod` panels, corner brackets, `mod-head`, chip nav, controls) from `templates/home.html` into `static/hud.css` + two Jinja partials. Then restructure `templates/journals.html` and `templates/breakdown.html` to include the partials and wrap their existing content in `.mod` panels, rewiring each page's JS to the new DOM. `home.html` is not touched.

**Tech Stack:** Flask + Jinja2 templates, vanilla JS, hand-written CSS. No build step. Fonts: JetBrains Mono + Bricolage Grotesque (Google Fonts). Verification: standalone Jinja render + `python3 -m http.server` + Playwright screenshot; live checks post-deploy.

## Global Constraints

- **Do NOT modify `templates/home.html`** or any dashboard route/JS — `hud.css` is derived from it, not shared with it.
- **No backend/route/API changes.** Pages consume the same endpoints they do today.
- **Preserve all existing functionality** on each page; rewire JS to new DOM, do not rewrite logic.
- **CSS link path is `/static/hud.css`** (plain path, no `url_for`) so templates render both in Flask and in the standalone preview harness.
- **Deploy:** edit files on the host at `/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/`; apply with `pct exec 101 -- systemctl restart ares`; propagate to ZEUS with `./deploy/sync-to-zeus.sh --restart`.
- **Commits:** subject line only — no body, no `Co-Authored-By` trailer (repo rule).
- **Op modules are ARES-only** (`#panel-ops` hides when `docker===1`); pages set `data-brand="ARES"` but must stay theme-token-driven.

## Preview harness (used by every task's test step)

Render a template standalone (mock Jinja context), write it into the project root so
`/static/hud.css` resolves, serve the project root, screenshot with Playwright.

```bash
# From /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD
python3 - <<'PY'
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader("templates"))
html = env.get_template("PAGE.html").render(boot={"hostname": "ARES"})
open("_preview.html", "w").write(html)
print("rendered", len(html))
PY
# serve project root (so /static/hud.css and /_preview.html both resolve)
(python3 -m http.server 8765 --bind 0.0.0.0 >/tmp/prev.log 2>&1 &)
# then navigate Playwright to http://100.77.42.110:8765/_preview.html and screenshot.
# stop server with: fuser -k 8765/tcp   (on its own line — never chained with &&)
# cleanup: rm -f _preview.html
```

Playwright runs against the ARES host IP `100.77.42.110`. A passing test = the expected
panels/brackets/nav render themed, and `browser_console_messages` shows no errors.

## File structure

- **Create** `static/hud.css` — theme tokens, fonts, reset, atmosphere, and reusable components (`.mod`, brackets, `mod-head`, chip nav, buttons, inputs, search, chips, meter bars, scrollbars). Single responsibility: the shared visual system.
- **Create** `templates/_hud_head.html` — `<head>` contents (meta, fonts, `hud.css` link, `data-brand`). Included by each page.
- **Create** `templates/_hud_nav.html` — the top nav bar (brand chip · page name · back-to-home · jump-to-ZEUS). Parameterized by `page_name`.
- **Modify** `templates/journals.html` — replace its inline theme CSS + head + nav with the kit; wrap sections in `.mod` panels; rewire JS selectors.
- **Modify** `templates/breakdown.html` — same treatment; hero/controls/pick-cards as panels.
- **Throwaway** `_preview.html` (git-ignored / deleted after each check) — never committed.

---

## Task 1: `hud.css` — tokens, fonts, reset, atmosphere

**Files:**
- Create: `static/hud.css`

**Interfaces:**
- Produces: the CSS custom properties (`--ares`, `--bg-*`, `--text-*`, `--amber`, `--ember-dim`, `--dur`, `--ease-out`, `--sat/--sab`, etc.) and base `html,body` styling that all later tasks and pages rely on. Brand switch via `html[data-brand="ZEUS"]`.

- [ ] **Step 1: Create `static/hud.css` with the token + base block** (copied verbatim from `home.html`, ARES `:root` + ZEUS override + base):

```css
/* hud.css — shared HUD design system, derived from home.html. Do not import into home.html. */
:root {
    --bg-0:#070403; --bg-1:#0d0605; --bg-2:#140807; --bg-3:#1c0c09;
    --ares:#f5402d; --ares-rgb:245,64,45; --panel-rgb:20,8,7; --ares-bright:#ff7a45; --ember:#c2301f; --ember-dim:rgba(var(--ares-rgb),.28);
    --grid:rgba(var(--ares-rgb),.055);
    --text-1:#f6ece9; --text-2:#c99a90; --text-3:#8d6259; --text-4:#5a3d37;
    --green:#10b981; --amber:#f59e0b; --red:#ef4444;
    --dur-fast:120ms; --dur:180ms; --dur-slow:280ms;
    --ease-out:cubic-bezier(.16,1,.3,1);
    --sat:env(safe-area-inset-top,0px); --sab:env(safe-area-inset-bottom,0px);
}
html[data-brand="ZEUS"] {
    --bg-0:#03060b; --bg-1:#060b14; --bg-2:#0a1220; --bg-3:#0e1a2e;
    --ares:#38bdf8; --ares-rgb:56,189,248; --ares-bright:#a78bfa; --ember:#6366f1;
    --text-1:#e9f2fb; --text-2:#90aecb; --text-3:#5a7690; --text-4:#374a5e;
    --amber:#a855f7; --panel-rgb:10,18,32;
}
* { margin:0; padding:0; box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
html, body {
    min-height:100%; background:var(--bg-0); color:var(--text-1);
    font-family:'JetBrains Mono', monospace;
    -webkit-font-smoothing:antialiased; -moz-osx-font-smoothing:grayscale;
}
::selection { background:var(--ares); color:var(--bg-0); }
::-webkit-scrollbar { width:9px; height:9px; }
::-webkit-scrollbar-thumb { background:rgba(var(--ares-rgb),.28); }
::-webkit-scrollbar-track { background:transparent; }
```

- [ ] **Step 2: Test — the tokens resolve.** Create a throwaway probe and screenshot:

```bash
cat > _preview.html <<'HTML'
<!doctype html><html data-brand="ARES"><head><link rel="stylesheet" href="/static/hud.css"></head>
<body style="padding:24px"><div id="p" style="color:var(--ares)">ARES token check</div></body></html>
HTML
(python3 -m http.server 8765 --bind 0.0.0.0 >/tmp/prev.log 2>&1 &)
```

Navigate Playwright to `http://100.77.42.110:8765/_preview.html`, run
`browser_evaluate` returning `getComputedStyle(document.getElementById('p')).color`.
Expected: `rgb(245, 64, 45)` (ARES red). Switch `data-brand="ZEUS"` → expected `rgb(56, 189, 248)`.

- [ ] **Step 3: Stop server + clean up**

```bash
fuser -k 8765/tcp
rm -f _preview.html
```

- [ ] **Step 4: Commit**

```bash
git add static/hud.css
git commit -m "Add hud.css foundation: theme tokens, reset, scrollbars"
```

---

## Task 2: `hud.css` — components (panels, nav, controls, meters)

**Files:**
- Modify: `static/hud.css` (append)

**Interfaces:**
- Consumes: tokens from Task 1.
- Produces: class contracts used by all page rebuilds — `.hud-shell`, `.hud-top`, `.chip`/`.chip-mark`/`.chip-name`/`.chip-tag`/`.chip.is-active`, `.mod` + `.c-tl/.c-tr/.c-bl/.c-br`, `.mod-head`/`.mod-title`/`.mod-meta`, `.mod-body`, `.btn`, `.field`/`.field-search`, `.chiptag`, `.meter`/`.meter-fill`.

- [ ] **Step 1: Append the component block to `static/hud.css`** (panels + brackets + heads copied verbatim from `home.html`; nav/controls adapted):

```css
/* ---- shell + top nav ---- */
.hud-shell { position:relative; z-index:1; display:flex; flex-direction:column;
             min-height:100vh; min-height:100dvh; padding:calc(14px + var(--sat)) 16px calc(14px + var(--sab)); gap:14px; }
.hud-top { display:flex; align-items:center; justify-content:space-between; gap:16px; flex-wrap:wrap; }
.hud-top > * { min-width:0; max-width:100%; }
.nodes { display:flex; gap:8px; flex-wrap:wrap; }
.chip { display:flex; align-items:center; gap:8px; padding:6px 12px; text-decoration:none;
        border:1px solid var(--ember-dim); color:var(--text-2); transition:all var(--dur) var(--ease-out); }
.chip-mark { display:grid; place-items:center; width:17px; height:17px; font-size:10px; font-weight:700; border:1px solid currentColor; }
.chip-name { font-size:11px; font-weight:700; letter-spacing:.11em; }
.chip-tag  { font-size:9.5px; letter-spacing:.1em; opacity:.72; }
.chip.is-active { background:rgba(var(--ares-rgb),.07); border-color:var(--ares); color:var(--ares);
                  box-shadow:0 0 16px -7px var(--ares), inset 0 0 22px -14px var(--ares); }
.chip.is-active .chip-mark { background:rgba(var(--ares-rgb),.14); }
.chip:not(.is-active):hover { border-color:var(--ares); color:var(--text-1); background:rgba(var(--ares-rgb),.1); }

/* ---- panels ---- */
.mod { position:relative; contain:layout paint; border:1px solid var(--ember-dim); background:rgba(var(--panel-rgb),.55);
       box-shadow:inset 0 0 22px rgba(var(--ares-rgb),.045); }
.mod::before, .mod::after { position:absolute; width:11px; height:11px; border:2px solid var(--ares); pointer-events:none; }
.c-tl::before { content:''; top:-2px; left:-2px; border-right:0; border-bottom:0; }
.c-tr::before { content:''; top:-2px; right:-2px; border-left:0; border-bottom:0; }
.c-bl::after  { content:''; bottom:-2px; left:-2px; border-right:0; border-top:0; }
.c-br::after  { content:''; bottom:-2px; right:-2px; border-left:0; border-top:0; }
.mod-head { display:flex; align-items:center; justify-content:space-between; gap:10px;
            padding:9px 12px; border-bottom:1px solid rgba(var(--ares-rgb),.14); }
.mod-title { font-size:13px; letter-spacing:.16em; color:var(--text-2); text-transform:uppercase; }
.mod-meta  { font-size:12px; letter-spacing:.1em; color:var(--text-3); }
.mod-body  { padding:14px; }

/* ---- controls ---- */
.btn { font:inherit; font-size:12px; letter-spacing:.08em; text-transform:uppercase; cursor:pointer;
       color:var(--text-2); background:rgba(var(--ares-rgb),.06); border:1px solid var(--ember-dim);
       padding:8px 14px; transition:all var(--dur) var(--ease-out); }
.btn:hover { color:var(--text-1); border-color:var(--ares); background:rgba(var(--ares-rgb),.12); }
.field { font:inherit; font-size:13px; color:var(--text-1); background:var(--bg-1);
         border:1px solid var(--ember-dim); padding:9px 12px; width:100%; }
.field:focus { outline:none; border-color:var(--ares); }
.field-search { display:flex; align-items:center; gap:8px; background:var(--bg-1); border:1px solid var(--ember-dim); padding:0 12px; }
.field-search .field { border:0; background:transparent; padding:11px 0; }
.chiptag { display:inline-flex; align-items:center; gap:5px; font-size:10px; letter-spacing:.08em;
           color:var(--text-2); border:1px solid var(--ember-dim); padding:3px 8px; }

/* ---- meter bar (array/IO style) ---- */
.meter { position:relative; height:14px; background:rgba(var(--ares-rgb),.1); overflow:hidden; }
.meter-fill { height:100%; background:var(--ares); box-shadow:0 0 10px -2px var(--ares); transition:width var(--dur) var(--ease-out); }
```

- [ ] **Step 2: Test — a panel renders with brackets, head, and a meter.** Write the probe:

```bash
cat > _preview.html <<'HTML'
<!doctype html><html data-brand="ARES"><head><link rel="stylesheet" href="/static/hud.css"></head>
<body class="hud-shell">
  <div class="hud-top"><a class="chip is-active"><span class="chip-mark">A</span><span class="chip-name">ARES</span><span class="chip-tag">JOURNALS</span></a></div>
  <section class="mod c-tl c-tr c-bl c-br">
    <div class="mod-head"><span class="mod-title">Search Archive</span><span class="mod-meta">12 notebooks</span></div>
    <div class="mod-body"><div class="field-search"><input class="field" placeholder="search…"></div>
      <div class="meter" style="margin-top:12px"><div class="meter-fill" style="width:64%"></div></div>
      <button class="btn" style="margin-top:12px">Refresh</button></div>
  </section>
</body></html>
HTML
(python3 -m http.server 8765 --bind 0.0.0.0 >/tmp/prev.log 2>&1 &)
```

Navigate Playwright + screenshot. Expected: dark panel with 4 red corner brackets, an
uppercase head with red-tinted divider, a bordered search field, a red meter bar ~64%,
and a themed button. Console: no errors. Repeat with `data-brand="ZEUS"` → same, in blue.

- [ ] **Step 3: Stop server + clean up** — `fuser -k 8765/tcp` then `rm -f _preview.html`

- [ ] **Step 4: Commit**

```bash
git add static/hud.css
git commit -m "Add hud.css components: panels, brackets, nav chip, controls, meter"
```

---

## Task 3: `_hud_head.html` partial

**Files:**
- Create: `templates/_hud_head.html`

**Interfaces:**
- Produces: reusable `<head>` inner HTML. Pages include it inside their own `<head>`. Expects the page to set `<html data-brand="ARES">` and to have defined `{% set page_title %}` (optional; defaults to "ARES").

- [ ] **Step 1: Create `templates/_hud_head.html`:**

```html
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>{{ page_title | default('ARES') }}</title>
<link rel="icon" type="image/svg+xml" href="/static/favicon-ares.svg?v=2">
<meta name="theme-color" content="#04070d">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,400;12..96,500;12..96,600;12..96,700;12..96,800&family=JetBrains+Mono:wght@300;400;500;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/hud.css">
```

- [ ] **Step 2: Test — it renders and pulls in hud.css.** Standalone render + serve:

```bash
python3 - <<'PY'
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader("templates"))
open("_preview.html","w").write("<!doctype html><html data-brand='ARES'><head>"
  + env.get_template("_hud_head.html").render(page_title="JOURNALS · ARES")
  + "</head><body class='hud-shell'><section class='mod c-tl'><div class='mod-body'>ok</div></section></body></html>")
print("ok")
PY
(python3 -m http.server 8765 --bind 0.0.0.0 >/tmp/prev.log 2>&1 &)
```

Playwright: navigate, `browser_evaluate` returns `document.title` → expected
`"JOURNALS · ARES"`; the `.mod` panel is dark with a bracket (hud.css loaded). No console errors.

- [ ] **Step 3: Stop server + clean up** — `fuser -k 8765/tcp` then `rm -f _preview.html`

- [ ] **Step 4: Commit**

```bash
git add templates/_hud_head.html
git commit -m "Add _hud_head partial: shared head + hud.css link"
```

---

## Task 4: `_hud_nav.html` partial

**Files:**
- Create: `templates/_hud_nav.html`

**Interfaces:**
- Consumes: `.chip` styles from Task 2.
- Produces: the top nav row. Include with `{% with page_name='JOURNALS' %}{% include '_hud_nav.html' %}{% endwith %}`. Contains a back-to-home chip (`/`) and a jump-to-ZEUS chip (`/jump/hermes`).

- [ ] **Step 1: Create `templates/_hud_nav.html`:**

```html
<header class="hud-top">
  <nav class="nodes">
    <a class="chip is-active" href="/"><span class="chip-mark">A</span><span class="chip-name">ARES</span><span class="chip-tag">{{ page_name | default('') }}</span></a>
    <a class="chip" href="/"><span class="chip-name">← Home</span></a>
  </nav>
  <nav class="nodes">
    <a class="chip" href="/jump/hermes"><span class="chip-name">ZEUS →</span></a>
  </nav>
</header>
```

- [ ] **Step 2: Test — nav renders with the page name.** Render + serve:

```bash
python3 - <<'PY'
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader("templates"))
body = env.from_string("{% with page_name='JOURNALS' %}{% include '_hud_nav.html' %}{% endwith %}").render()
open("_preview.html","w").write("<!doctype html><html data-brand='ARES'><head>"
  + env.get_template("_hud_head.html").render() + "</head><body class='hud-shell'>" + body + "</body></html>")
print("ok")
PY
(python3 -m http.server 8765 --bind 0.0.0.0 >/tmp/prev.log 2>&1 &)
```

Playwright screenshot. Expected: a red active "A ARES JOURNALS" chip, a "← Home" chip,
and a right-aligned "ZEUS →" chip — all themed. No console errors.

- [ ] **Step 3: Stop server + clean up** — `fuser -k 8765/tcp` then `rm -f _preview.html`

- [ ] **Step 4: Commit**

```bash
git add templates/_hud_nav.html
git commit -m "Add _hud_nav partial: HUD top bar"
```

---

## Task 5: Journals — head/nav swap + panel scaffold

**Files:**
- Modify: `templates/journals.html`

**Interfaces:**
- Consumes: `_hud_head.html`, `_hud_nav.html`, hud.css classes.
- Produces: the new journals DOM: `<html data-brand="ARES">`, `.hud-shell` root, panels `#jr-search` (SEARCH ARCHIVE), `#jr-today` (ON THIS DAY), `#jr-archive` (ARCHIVE). Existing element IDs the JS uses (search input, shelf grid, results rail, on-this-day container, gallery, reader) are **kept by their current IDs** — only their wrappers change.

- [ ] **Step 1: Inventory the JS hooks.** Run and record every `getElementById`/`querySelector` selector the page's script uses, so wrappers can change without breaking them:

```bash
grep -nE "getElementById\(|querySelector(All)?\(|\.id ?=|id=\"" templates/journals.html | sed -n '1,80p'
```

Expected: a list of selectors (e.g. the search input id, shelf container id, rail id,
on-this-day id, gallery id, reader ids). Keep these IDs unchanged in later steps.

- [ ] **Step 2: Replace the `<head>` and top of `<body>`** — swap the page's inline `<style>`/head for the partials and open the shell. Set `<html lang="en" data-brand="ARES">`. Inside `<head>` put `{% include "_hud_head.html" %}` with `{% set page_title = "JOURNALS · ARES" %}` above it. Immediately inside `<body>` add `<div class="hud-shell">` and `{% with page_name='JOURNALS' %}{% include "_hud_nav.html" %}{% endwith %}`, and close `</div>` before `</body>`. Delete the page's own theme CSS block (tokens, body, navbar) — keep only *layout-specific* CSS (book-cover aspect ratios, reader positioning, rail scroll) inside a trimmed inline `<style>` for now.

- [ ] **Step 3: Wrap the three sections in panels** — put the search input + results rail inside:

```html
<section class="mod c-tl c-tr c-bl c-br" id="jr-search">
  <div class="mod-head"><span class="mod-title">Search Archive</span><span class="mod-meta" id="jr-count">—</span></div>
  <div class="mod-body"><!-- keep existing search field (same input id) + results rail (same id) --></div>
</section>
```

Wrap the on-this-day container in `<section class="mod c-tl c-br" id="jr-today">` with a
`mod-head` "On This Day"; wrap the shelf grid in `<section class="mod c-tl c-tr c-bl c-br" id="jr-archive">`
with a `mod-head` "Archive" + `<span class="mod-meta" id="jr-books">—</span>`. Move the
existing shelf/rail/on-this-day nodes inside these bodies unchanged (same IDs).

- [ ] **Step 4: Test — the journals shell renders themed.** Standalone render + serve (JS
will error on missing `/api`, that's expected; we only check the shell):

```bash
python3 - <<'PY'
from jinja2 import Environment, FileSystemLoader
env=Environment(loader=FileSystemLoader("templates"))
open("_preview.html","w").write(env.get_template("journals.html").render(boot={"hostname":"ARES"}))
print("ok")
PY
(python3 -m http.server 8765 --bind 0.0.0.0 >/tmp/prev.log 2>&1 &)
```

Playwright screenshot of `/_preview.html`. Expected: HUD nav bar, three bracketed
`.mod` panels (Search Archive / On This Day / Archive) with uppercase heads, dark theme.
Data areas may be empty (no API) — acceptable. `browser_console_messages`: only
network/fetch errors for `/api/...`, no CSS/JS reference errors (undefined selectors).

- [ ] **Step 5: Stop server + clean up** — `fuser -k 8765/tcp` then `rm -f _preview.html`

- [ ] **Step 6: Commit**

```bash
git add templates/journals.html
git commit -m "Journals: adopt HUD head/nav + wrap sections in mod panels"
```

---

## Task 6: Journals — reader modal HUD chrome + live parity

**Files:**
- Modify: `templates/journals.html`

**Interfaces:**
- Consumes: the panel scaffold from Task 5.
- Produces: the reader's top/bottom bars restyled with `.mod-head` chrome + `.btn`/bracket accents; the page-slider and reveal toggle themed. All reader JS (open, swipe, keyboard, slider, OCR reveal, prefetch) unchanged (same element IDs).

- [ ] **Step 1: Restyle the reader bars** — give the reader's top bar the `mod-head` look
  (uppercase title, red divider), make the close/reveal controls `.btn`, and restyle the
  page-range slider track/thumb to the accent via the trimmed inline `<style>`. Do not
  rename reader element IDs.

- [ ] **Step 2: Test — reader shell renders (forced open).** In the standalone render,
  Playwright `browser_evaluate` to remove the reader's `hidden`/display style, then
  screenshot. Expected: full-screen dark reader with a HUD-chromed top bar, themed
  slider, `.btn` controls. No CSS reference errors.

- [ ] **Step 3: Deploy to ARES + live parity check**

```bash
pct exec 101 -- systemctl restart ares
```

Then, logged in, load `http://100.77.42.110:8080/journals` in Playwright and verify each
preserved behavior still works: (a) typing a query shows the results rail; (b) clicking a
book opens the page gallery; (c) clicking a page opens the reader; (d) reader arrows /
slider change pages; (e) reveal toggle shows OCR text; (f) "On This Day" appears when
present. Record a screenshot of the live themed page.

- [ ] **Step 4: Commit**

```bash
git add templates/journals.html
git commit -m "Journals: HUD-chrome reader modal + slider"
```

---

## Task 7: Finance — head/nav swap + hero + controls

**Files:**
- Modify: `templates/breakdown.html`

**Interfaces:**
- Consumes: partials + hud.css.
- Produces: `<html data-brand="ARES">`, `.hud-shell`, `{% include _hud_nav %}` (page_name "FINANCE"), a `#fx-hero` `.mod` (TOP CONVICTION: top-pick + fact grid), and a `#fx-picks` `.mod` whose `mod-head` hosts the budget `<input class="field">` (same id the JS reads) and the Refresh `.btn`. Existing JS selectors (elite-picks container, budget input, refresh button, footer) kept by their current IDs.

- [ ] **Step 1: Inventory the JS hooks** (same as Journals Task 5, Step 1):

```bash
grep -nE "getElementById\(|querySelector(All)?\(|localStorage|id=\"" templates/breakdown.html | sed -n '1,80p'
```

Record the budget-input id, picks-grid id, refresh-button id, footer id, and the
localStorage key for the budget. Keep them unchanged.

- [ ] **Step 2: Swap head/nav + open shell** — identical treatment to Journals Task 5 Step
  2, with `page_title = "FINANCE · ARES"` and `page_name='FINANCE'`. Delete the page's
  theme CSS; keep only layout-specific rules (grid gaps, meter overlay) in a trimmed
  inline `<style>`.

- [ ] **Step 3: Rebuild hero + controls** — wrap the hero in:

```html
<section class="mod c-tl c-tr c-bl c-br" id="fx-hero">
  <div class="mod-head"><span class="mod-title">Top Conviction</span><span class="mod-meta" id="fx-asof">—</span></div>
  <div class="mod-body"><!-- top-pick ticker (large) + narrative | fact grid (budget/source/superinv/momentum/congress/analyst) --></div>
</section>
```

Wrap the ranked section head so it hosts controls:

```html
<div class="mod-head">
  <span class="mod-title">Ranked by Conviction</span>
  <span class="mod-meta" style="display:flex;gap:10px;align-items:center">
    $<input class="field" id="fx-budget" type="number" min="0" step="100" style="width:90px"><button class="btn" id="fx-refresh">Refresh</button>
  </span>
</div>
```

Keep `#fx-budget`/`#fx-refresh` matching whatever the existing JS reads (rename the JS
refs in one place if the old IDs differ — do it in this step, showing both edits).

- [ ] **Step 4: Test — finance shell renders themed** (standalone render of
  `breakdown.html`, same harness as Task 5 Step 4). Expected: HUD nav, TOP CONVICTION
  panel with a fact grid, a RANKED head with a `$` budget field + Refresh button. Empty
  picks area acceptable (no API). No CSS/JS reference errors.

- [ ] **Step 5: Stop server + clean up** — `fuser -k 8765/tcp` then `rm -f _preview.html`

- [ ] **Step 6: Commit**

```bash
git add templates/breakdown.html
git commit -m "Finance: HUD head/nav + hero panel + controls in mod-head"
```

---

## Task 8: Finance — pick cards, deep-research, live parity

**Files:**
- Modify: `templates/breakdown.html`

**Interfaces:**
- Consumes: the scaffold from Task 7.
- Produces: each pick rendered as a mini `.mod` card (rank + ticker + name, big conviction number, a `.meter`/`.meter-fill` conviction bar, elite/heavy counts, firms, `.chiptag` chips, a deep-research `.btn`); the deep-research expansion restyled with a `.mod`/`mod-head` and bull/bear lists; footer themed. The picks-render JS is updated to emit this markup.

- [ ] **Step 1: Update the pick-card renderer** — in the page's JS that builds each card,
  change the emitted HTML to the mini-panel structure below (keep the data fields and the
  deep-research button's existing handler/id scheme):

```html
<article class="mod c-tl c-br fx-card">
  <div class="mod-body">
    <div class="fx-card-top"><span class="fx-rank">01</span><a class="fx-tk">NVDA</a><span class="fx-co">NVIDIA</span></div>
    <div class="fx-score">92</div>
    <div class="meter"><div class="meter-fill" style="width:92%"></div></div>
    <div class="fx-firms"><!-- firms --></div>
    <div class="fx-chips"><span class="chiptag">+4%</span><span class="chiptag">upside 18%</span></div>
    <button class="btn fx-deep">Deep research →</button>
    <div class="fx-research" hidden><!-- summary + bull/bear, filled async --></div>
  </div>
</article>
```

Add the small layout-only rules (`.fx-card-top`, `.fx-rank`, `.fx-score`, `.fx-firms`,
`.fx-chips`, `.fx-research`) to the trimmed inline `<style>` — sizes/spacing only, colors
from tokens. The conviction meter width binds to the pick's score as today.

- [ ] **Step 2: Test — a stubbed pick card renders.** In the standalone preview, use
  Playwright `browser_evaluate` to call the page's card-render function with one mock
  pick object (shape from `/api/elite-picks`: `{ticker, name, rank, llm_score, momentum,
  upside, firms:[]}`), then screenshot. Expected: a bracketed card with rank, ticker, big
  red score, a red conviction meter, chips, and a Deep-research button.

- [ ] **Step 3: Deploy + live parity check**

```bash
pct exec 101 -- systemctl restart ares
```

Logged in, load `http://100.77.42.110:8080/breakdown` in Playwright and verify: (a) picks
grid populates from `/api/elite-picks`; (b) editing the budget re-renders allocations and
persists (reload keeps it); (c) Refresh refetches; (d) Deep-research expands and fills
with summary + bull/bear after polling; (e) footer timestamp updates. Screenshot the live
themed page.

- [ ] **Step 4: Commit**

```bash
git add templates/breakdown.html
git commit -m "Finance: pick cards as HUD panels with conviction meters + deep-research"
```

---

## Task 9: Propagate to ZEUS + home.html regression check

**Files:** none (deploy + verify only)

- [ ] **Step 1: Sync the kit + pages to ZEUS**

```bash
./deploy/sync-to-zeus.sh --restart
```

- [ ] **Step 2: Regression — home dashboard unchanged.** `home.html` is untouched *by
  construction* — no task in this plan lists it as a Modify target. (Note: the repo
  already carries pre-existing uncommitted `home.html` changes from prior dashboard work;
  those are unrelated to this plan and must NOT be committed here.) Confirm this plan
  added no new `home.html` edits, then verify visually: load
  `http://100.77.42.110:8080/` in Playwright, screenshot, confirm the dashboard is
  identical (panels, wordmark, colors) with no new console errors.

- [ ] **Step 3: Cross-check the two rebuilt pages once more** live at `/journals` and
  `/breakdown` — themed, functional, matching the dashboard's HUD language.

- [ ] **Step 4: Confirm all work is committed.** Each task committed its own files
  explicitly (`git add <specific paths>`) — **never `git add -A`/`git add .`** here, as
  that would sweep up the unrelated pre-existing `home.html` changes. Verify only the
  intended paths were committed:

```bash
git log --oneline -8
git status --porcelain static/hud.css templates/_hud_head.html templates/_hud_nav.html templates/journals.html templates/breakdown.html
# expected: the log shows the per-task commits; status shows these paths clean (committed)
```

---

## Self-review notes

- **Spec coverage:** Phase 0 kit → Tasks 1–4; Journals rebuild → Tasks 5–6; Finance
  rebuild → Tasks 7–8; "home.html untouched" + ZEUS propagation → Task 9. Verification
  approach (harness pre-deploy, live post-deploy) is embedded in each task's test step.
  Terminal + Photos are out of scope (future specs) — no tasks, as intended.
- **Preserved-functionality checks** are explicit live steps (Journals Task 6 Step 3;
  Finance Task 8 Step 3).
- **Class contract** is consistent across tasks: `.hud-shell`, `.mod`+`.c-*`,
  `.mod-head/.mod-title/.mod-meta/.mod-body`, `.chip*`, `.btn`, `.field/.field-search`,
  `.chiptag`, `.meter/.meter-fill` — defined in Tasks 1–2, consumed in 5–8.
- **No `url_for`** anywhere (plain `/static/hud.css`) so the preview harness renders.
