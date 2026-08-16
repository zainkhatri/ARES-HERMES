# Dashboard perf overhaul + Operation-modules redesign — design

**Date:** 2026-08-16
**Trigger:** Dashboard "runs extremely heavy," graphs low-fps, and **slows the whole
Mac down when swiping Spaces/Mission Control**. Plus: redesign the Operation modules
to fit the new (ARES-red / ZEUS-blue) aesthetic.

## Root cause (LLM council, unanimous)
The window is **forever-dirty**: full-screen `position:fixed` layers that animate
every frame — `body::after` (`ember-breathe` opacity on two radial-gradients),
`mark-pulse` (wordmark), a `drop-shadow` filter on animated gauge arcs, plus the
full-screen `.scanline` at z-index 900. macOS animates a Spaces swipe by compositing
a cached window snapshot; a window that never holds still can't be cached, so it
contends with WindowServer for GPU raster/VRAM → the whole machine stutters. The 1s
`setInterval`/SVG rebuild/count-up tweens are a rounding error by comparison.

**Council corrections to the first plan:**
- "Pause when hidden" does NOT fix the swipe (a Spaces swipe doesn't fire
  `visibilitychange`) — keep it, but only for background cost, not as the headline.
- `content-visibility:auto` is unsafe on a single-screen no-scroll layout → use
  `contain:layout paint` on `.mod`.
- Throttling charts / dropping tweens ≈ noise for the swipe; do them for general
  lightness, not as the cure.
- Don't `will-change`/promote the static `.scanline` — flatten animations instead.

## Perf plan (ranked by impact)
1. **Kill `ember-breathe`** → `body::after` static opacity. (≈80% of the relief.)
2. **Kill `mark-pulse`** (wordmark) and remove the `drop-shadow` filter on the arc.
3. **`contain:layout paint`** on every `.mod` panel (isolate repaints).
4. **Drop count-up `tweenNum`** — set number text directly (removes rAF churn); poll
   1s→2s.
5. **Page-Visibility gate** the poll (skip work while hidden; refetch on return).
6. Verify skeleton `shimmer` stops after data load (no lingering animated skeletons).
7. If graphs still read low-fps after 1–3, morph the chart `<path>` via CSS
   `transition` between 2s samples (smooth, zero per-frame JS) — not a canvas rewrite.

## Operation-modules redesign (ARES + ZEUS)
Current "Operation modules" = a plain vertical nav list (Photos/Terminal/Journals/
Finance) styled with the old warm palette. Redesign to the new aesthetic: brand-
accent (red on ARES, blue on ZEUS via the existing `--ares`/`--ares-rgb` vars),
tighter cards, clear icon + label + arrow, hover lift, and fill the column cleanly.
Must stay capability-gated (ZEUS hides Photos/Journals). Also finish the theme: make
the vitals tile accent colors brand-aware (they're hardcoded warm today, so ZEUS
tiles still look orange/red).

## Ship order
Perf fixes → verify (render + no-regression) → commit+deploy both boxes. Then
Operation-modules redesign + tile-color theming → verify → ship. Then (lower
priority) ZEUS-specific data panels (scheduled jobs, per-SSD array map) if time.
