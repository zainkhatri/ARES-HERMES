# Phase 2 — Content-Text Search (design)

**Date:** 2026-08-19
**Status:** Approved (brainstorm). Extends the Phase-1 file locator so the assistant can search *inside* documents and quote them. Lighter single-pass build.

## Goal
Let the ask assistant answer from the CONTENTS of documents (not just names + a one-file peek): "find the doc that mentions net-30", "what does the ibtakar runbook say about X". Keyword full-text over extracted document text, feeding matching passages to qwen.

## Scope
- **Extract:** `.pdf` (~1206, via PyMuPDF), `.md` (~751), `.txt` (~1005). ~3K files total. **Skip `.docx`** (only 7 — not worth a python-docx dependency; revisit if needed).
- The allowlist naturally excludes code (`.py/.js/...`); vendored/junk/backup dirs are already pruned by the Phase-1 walk (`JUNK_DIRS`/`BACKUP_DIRS`/dotfiles).
- **Not** semantic (that's Phase 3 / Qdrant). Keyword FTS5 only.

## Architecture (extend `system/files_index.py`)
- New FTS5 table in the same index DB: **`content_fts(path UNINDEXED, body)`** — one row per extracted doc.
- Constants: `CONTENT_EXTS = {".pdf", ".md", ".markdown", ".txt"}`, `CONTENT_CHAR_CAP = 40000` (per-file extracted text cap), `CONTENT_FILE_MAX = 20_000_000` (skip files larger than 20MB before extracting — avoids parsing giant PDFs).
- `_extract_text(abspath, ext) -> str`:
  - `.pdf` → PyMuPDF: open, accumulate `page.get_text()` across pages until `CONTENT_CHAR_CAP`, close. Any error → `""`.
  - `.md/.markdown/.txt` → `open(errors="ignore").read(CONTENT_CHAR_CAP)`.
  - Never raises; returns capped text (or "").
- `build_index()` extended: create `content_fts`; during the existing throttled file loop, when `ext in CONTENT_EXTS` and `size <= CONTENT_FILE_MAX`, extract text and append to a `cbatch`; flush `cbatch` into `content_fts` in batches with a small `sleep` (PDF parsing is the heaviest step — throttle it). Bounded by the ~3K doc count.
- **Wedge-safety:** doc-exts only, per-file char cap, size cap, throttled flushes, bounded count. Same discipline as the file walk; ~3K docs finish quickly.

## Search + ask integration
- `content_search(query, limit=6, db) -> [{path, kind, snippet}]`: FTS `MATCH` on `content_fts` (same stopword-filtered prefix terms), `ORDER BY rank`, returns the matching passage via FTS5 `snippet(content_fts, 1, '[', ']', ' … ', 14)`; `kind` via `kind_for`.
- `files_ask` route (both boxes): run `files_index.search` (names) **and** `content_search` (bodies). Build a `PASSAGES FROM DOCUMENTS` block (`<path>: <snippet>`) added to the qwen prompt. Merge content-match paths into `locations` (deduped by path, content hits first so "inside" queries surface the doc), each as `{name, path, kind}` for the chips. Keep the Phase-1 folder peek.
- `sys_p` updated: "If PASSAGES FROM DOCUMENTS are given, use them to answer and cite the doc path. Quote briefly."
- Grounding unchanged: chips + passages come from the index, never invented.

## Data flow
`query → files_index.search (names) + content_search (bodies) → merge candidates + passages → qwen grounded prompt → answer + chips (content hits marked)`.

## Build / freshness
The existing nightly reindex (`ops/run-reindex.sh` on ARES, cron curl on ZEUS) rebuilds `content_fts` alongside `files_fts` — no new job. Manual "Reindex" button covers it.

## Testing
Extend `tests/test_files_api.py`: build over a tmp tree with a `.md` and `.txt` containing a known phrase (e.g. "net-30 payment terms"); assert `content_search("net-30")` returns that path with a snippet containing the phrase; assert a code file's contents are NOT indexed (`.py` not in CONTENT_EXTS). PDF extraction verified live (generating a PDF in a unit test needs a writer lib — out of scope; PyMuPDF read path is exercised live on the real pool).

## Both boxes
Same code; ZEUS `files_index.py` is the self-contained variant (import `files_api`, repo-root `_APP_ROOT`). ZEUS ask route mirrors the passage integration.

## Out of scope
Semantic embeddings / Qdrant vector search = Phase 3. `.docx`/`.rtf`/Pages extraction. Per-file incremental re-extraction (full rebuild nightly is fast enough at ~3K docs).
