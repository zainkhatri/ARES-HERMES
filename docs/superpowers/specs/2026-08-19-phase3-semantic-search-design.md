# Phase 3 — Semantic Search (design)

**Date:** 2026-08-19
**Status:** Approved (brainstorm). Adds meaning-based retrieval on top of Phase 1 (names) + Phase 2 (keyword content). Lighter single-pass build.

## Goal
Answer by MEANING, not just matching words: "the pricing doc" surfaces a cost-structure doc that never says "pricing." Embed document text, cosine-retrieve the closest chunks, feed them (with the keyword hits) to qwen.

## Decisions (from brainstorm)
- **Vector store:** a numpy `.npy` matrix + cosine — same convention as the photo CLIP search (`ai_data/clip_embeddings.npy`). No new service (Qdrant stays dormant). Right-sized for ~29K chunks.
- **Embedding model:** `nomic-embed-text` (768-dim) on the host Ollama, served to both boxes. Query + docs embedded by the same model.
- **Hybrid retrieval:** semantic augments (does not replace) the Phase-2 keyword content search.
- **Corpus:** reuse the already-extracted `content_fts` bodies (2,628 docs ARES / 227 ZEUS) — no re-walk.

## Architecture (extend `system/files_index.py`)
Constants: `EMB_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")`, `OLLAMA_HOST = os.getenv("OLLAMA_HOST", ...)` (already set per box), `EMB_FILE = <APP_ROOT>/ai_data/doc_embeddings.npy`, `CHUNKS_FILE = <APP_ROOT>/ai_data/doc_chunks.json`, `CHUNK_SIZE=500`, `CHUNK_OVERLAP=80`, `EMB_BATCH=64`.

- `_chunk(text) -> [str]`: slice into ~500-char windows with 80 overlap; skip empty. Bounded by text length.
- `_embed(texts) -> list[list[float]]`: POST Ollama `/api/embed` `{model, input: texts}` (batched by `EMB_BATCH`), stdlib urllib (no `requests` dep on ZEUS), timeout, returns vectors. Any error → raises (caller handles).
- `embed_docs(db=INDEX_DB) -> int`: read `(path, body)` from `content_fts`; chunk all; embed in batches (throttled); L2-normalize; write `doc_embeddings.npy` (float32 `[N, 768]`) + `doc_chunks.json` (`[{path, chunk}]`, parallel rows) atomically (`.tmp` → `os.replace`). Returns chunk count. Skips gracefully (logs, no crash) if the embed model is unavailable.
- `semantic_search(query, k=8, db=INDEX_DB) -> [{path, chunk, score}]`: embed the query; `np.load` the matrix (mmap) + chunks; cosine (dot, since normalized); top-k; dedupe repeated paths keeping best score. Returns [] if the `.npy`/`.json` don't exist yet.

`start_reindex()`'s background `_run` calls `build_index()` then `embed_docs()` (best-effort) so the nightly cron covers both. Manual "Reindex" too.

**Wedge/perf:** embedding ~29K chunks is the one heavy step — batched (64), throttled, GPU-accelerated when the 3080 is home (CPU fallback when on loan). Query-time cosine over the matrix is instant. numpy required (present on ARES; install into ZEUS venv if missing).

## Ask integration (both boxes)
`files_ask` adds a third signal: `sem = files_index.semantic_search(query, k=6)`. Merge semantic chunks into the qwen context under `RELEVANT PASSAGES (semantic)` alongside the Phase-2 keyword `PASSAGES`; dedupe by path. Content/semantic hit paths populate the chips (marked `via:"content"`/`via:"semantic"`). Prompt already instructs: use passages to answer + cite; never invent paths.

## Data flow
`query → embed → cosine top-k chunks (+ keyword passages + name candidates) → merge → qwen → answer + chips`.

## Testing
Extend `tests/test_files_api.py` with a **monkeypatched `_embed`** (deterministic tiny vectors — no network): build content, `embed_docs`, then `semantic_search` returns the expected nearest chunk; assert `.npy`/`.json` written and `semantic_search` returns [] before embedding. (Live paraphrase check — "pricing" → cost doc — verified against the real corpus.)

## Both boxes
Same `files_index.py` (ZEUS self-contained variant). ZEUS embeds its 227 docs via ARES's Ollama (nomic) over Tailscale, stores its own `.npy`/`.json`. `.gitignore` already covers `ai_data/*` index artifacts — add `doc_embeddings.npy`/`doc_chunks.json`.

## Out of scope
Qdrant; re-embedding only changed docs (full nightly rebuild is fine at this size); embedding non-doc files or images (CLIP already covers photos separately).
