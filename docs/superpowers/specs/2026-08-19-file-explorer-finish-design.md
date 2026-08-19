# File Explorer — Phase 1 Finish (polish + brain upgrade)

**Date:** 2026-08-19
**Status:** Approved (brainstorm). Lighter single-pass execution — micro-fixes on shipped, proven code + a config change. Phase 2 (content extraction) deferred to its own cycle.

## Goal
Get the Phase-1 file explorer + llama assistant to ship-quality on both boxes (ARES ember / ZEUS blue) and upgrade the model from llama3.2:3b → qwen2.5:7b-instruct.

## Scope

### Polish (each a small, isolated fix)
1. **Prose prefix-leak** — the 3b model sometimes echoes `CONTENT OF TOP MATCH:` / `CANDIDATES:` at the start of its answer. Strip a leading such line server-side in `files_ask` before returning `answer`. (Belt-and-suspenders; the model change should also reduce it.)
2. **Console 404s** — `files.html` references `/static/favicon-ares.svg` + `/static/manifest.json`; on ZEUS the favicon path 404s. Use a brand-neutral existing favicon (`/static/favicon.svg`) and only reference a manifest that exists (or drop the manifest tag). Confirm 0 console errors on both boxes.
3. **Gitignore** — add `system/_files_thumb_cache/` and `ai_data/files_index.db*` to `.gitignore` (runtime artifacts; must never be tracked).
4. **A11y** — the "Filter this folder" input and the Ask input get associated `<label>`s (`sr-only`), so screen readers announce them.
5. **Ranking edge** — an exact top-level project-dir match (e.g. `PROJECTS/ARES-DASHBOARD`) should outrank a deep stray file matching the same words (e.g. `.../A&N/ARES/DashboardHome.swift`). Already partly handled by the depth penalty + segment bonus; add a small extra boost when a query term matches a **top-level** (depth-1) directory segment.
6. **Verify** — end-to-end sweep on ARES + ZEUS: ask (conceptual + recency + locate), browse, thumbnails, grid/list, theme, 0 console errors.

### Brain upgrade
- Pull **qwen2.5:7b-instruct** on the ARES host Ollama (serves both boxes). Fits the RTX 3080 (10GB) on GPU; ~4.7GB. Degrades to CPU (46GB RAM) when the GPU is on loan.
- Set `OLLAMA_MODEL=qwen2.5:7b-instruct` in ARES `.env` and ZEUS `.env`; restart both.
- Leave the existing Claude path intact (auto-used if `ANTHROPIC_API_KEY` is set) — no key today.

### Out of scope (own cycle)
- **Phase 2** content-text extraction (read inside docs/PDFs). Deferred to a fresh brainstorm→spec→plan.
- **Git landing** of `app.py`/`home.html` — stays uncommitted per the operating norm; ZEUS-DASH remains non-git (live deploy).

## Files touched
- `app.py` (ARES): `files_ask` prose-strip; `files_index` top-level-dir boost lives in `system/files_index.py`.
- `system/files_index.py`: depth-1 dir boost in `_score`.
- `templates/files.html`: favicon/manifest refs + `<label>`s.
- `.gitignore`: cache + index artifacts.
- ARES `.env` + ZEUS `.env`: `OLLAMA_MODEL`.
- ZEUS mirror of `app.py`/`files_index.py`/`files.html` edits.

## Verification
- `python3 -m pytest tests/test_files_api.py` green.
- Both boxes: `/files` loads with 0 console errors; ask returns clean prose (no prefix leak) with correct grounded chips; `MORDOR`/"newest resume"/"what is ares dashboard" answer well on the new model.
