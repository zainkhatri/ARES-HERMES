# Operation Modules — Unified UI Design

Date: 2026-09-12
Status: shipping (autonomous — owner away, asked to "ship something to see when I get back")

## Problem
The five operation modules (Photos, Terminal, Journals, Finance, Files) each have a
different look. Audit findings:

- **3 different nav headers**: home uses `.hud-top .chip`; photos uses a bespoke
  `.chrome` bar; files uses a bespoke `.navbar .nbr`.
- **Finance is the wrong color** — it overrides the warm palette with a cool BLUE one
  (`--bg-0:#04070d`, `--ares:#ef4444`, `--text-1:#e8f0f8`).
- **Journals drifted** — renamed tokens (`--ink`, `--accent`), panel opacity 0.9 vs the
  house 0.55, plus a big Bricolage `.jr-hero` title.
- **No ASCII wordmark on any sub-page** — the home dashboard's signature is the two-tone
  ANSI-Shadow ASCII wordmark; sub-pages have none.
- Owner dislikes the redundant `[A] ARES · <TAG>` chip sitting next to the Home button.

## Reference design system (home.html / hud.css)
Warm-red tokens, JetBrains Mono + Bricolage, sharp `.mod` panels with corner brackets
(`.c-tl/.c-tr/.c-bl/.c-br`), segmented meter bars, two-tone glowing ASCII wordmark.

## Design
One shared **module header band** replaces all three nav patterns:

```
[← HOME]      ██████╗ ...  PHOTOS (two-tone ASCII: brand half + off-white half, glow)
```

- Left: a single `← HOME` chip (hud.css `.chip` style). **The `[A] ARES` chip is removed.**
- Identity: a compact two-tone ANSI-Shadow ASCII wordmark of the *module name*
  (PHOTOS / TERMINAL / JOURNALS / FINANCE / FILES), mirroring home's ARES wordmark.
  Font `clamp(5px,1.05vw,10px)` so the 6-line block is a ~55px band.
- Right: each page keeps its own functional controls (mode tabs, hamburger, refresh…).

### Wiring
- `_hud_nav.html` → rewritten to render the band. Terminal / Finance / Journals already
  `{% include %}` it, so they update with zero per-page edits.
- Photos / Files don't load hud.css and have controls in the same bar → inline the ASCII
  mark + a small scoped style into their existing header, drop the `ARES`/`nbr` chip.
- **Finance**: delete the blue `:root` override so hud.css warm tokens win.
- **Journals**: drop the redundant `.jr-hero` H1 title (ASCII now carries identity);
  align panel opacity to the house value.

### Non-goals (deliberate, YAGNI)
- Not re-theming page *bodies* (gallery, bookshelf, file tree, terminal, finance cards) —
  they stay functional; only palette + header get unified this pass. Body harmonization can
  follow once the header lands and looks right.

## Verification
Restart `ares`, screenshot all five pages headless (Bearer auth), confirm: same header,
ASCII present, warm palette everywhere, no `ARES` chip.
