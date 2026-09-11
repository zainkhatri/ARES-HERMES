# Photos Recycle Bin — Migrate Legacy Bin + Review Section

Date: 2026-09-04
Status: Approved (design), pending implementation-plan

## Problem

Two separate trash piles exist on ARES, and neither is viewable in the Photos gallery:

1. **`PHOTOS/RECYCLE_BIN`** (legacy, ~8.4 GB) — an old deletion scheme. Structure:
   `RECYCLE_BIN/PHOTOS/<subpath>` holds 2,114 original photos; `RECYCLE_BIN/_thumbs/`
   holds 4,228 pre-made thumbnails. Last write Feb 2026. The photo scanner lists it in
   `SKIP_DIRS`, so nothing in the UI can see it. It is orphaned — no route reads it.
2. **`.ares-trash`** (current, `PROJECTS/.ares-trash/`) — where a photo goes today when
   the user trashes it in the gallery. Each item = the file plus a `<name>.meta.json`
   holding `original_path`, `trash_name`, `trashed_at`, `size`. Backend routes exist
   (`list_trash`, `restore`) but there is no Photos gallery section wired to them.

Goal: consolidate the legacy bin into the current bin, then delete the legacy directory,
and add a Photos "Recycle Bin" section so trashed photos can be reviewed, restored, or
purged.

## Verified facts (probed on real data, not assumed)

- **Legacy original path is reconstructable.** `RECYCLE_BIN/PHOTOS/<subpath>` means the
  original lived at container path `/mnt/data/PROMETHEUS/PHOTOS/<subpath>` (the format
  `.ares-trash` meta uses).
- **Legacy thumb naming is `thumbs_<md5(subpath)>.jpg`** and `thumbs_hq_<md5(subpath)>.jpg`,
  where `subpath` is the path relative to `PHOTOS/` (e.g. `md5("S95/2025/IMG_34267.jpg")`).
  Confirmed by matching one original to its thumb. Deterministic — no mtime/size input.
- **Current live-gallery thumb naming is different** (`sha1(abspath|mtime|size)`, app.py:6127).
  This is unrelated to the legacy thumbs and is not touched by this work.
- **Both bins are on the same filesystem** (`/mnt/nvme`), so migration moves are instant
  renames and consume no extra disk.

## Part A — Migration (one-time script)

`scripts/migrate_recycle_bin.py`, runnable once, idempotent, verify-before-delete.

For each of the 2,114 originals under `RECYCLE_BIN/PHOTOS/<subpath>`:

1. Reconstruct `original_path = /mnt/data/PROMETHEUS/PHOTOS/<subpath>`.
2. Choose `trash_name = <YYYYMMDD_HHMMSS from file mtime>_<basename>`; if the target name
   already exists in `.ares-trash`, append `_<n>` until unique.
3. Move the original file to `.ares-trash/<trash_name>`.
4. Write `.ares-trash/<trash_name>.meta.json` = `{original_path, trash_name,
   trashed_at (ISO from mtime), size}` — same shape `list_trash`/`restore` already read.
5. Copy the matching legacy thumb (`thumbs_<md5(subpath)>.jpg`, prefer the `_hq` variant)
   to `.ares-trash/_thumbs/<trash_name>.jpg` if it exists. Missing thumb is non-fatal
   (review section falls back to on-demand generation for that item).

After all items processed:
- Print a summary: migrated N originals, M thumbs carried, K missing thumbs.
- **Verify**: every migrated item has a readable meta.json whose file exists. Only if the
  count of verified new items >= count of legacy originals, delete `PHOTOS/RECYCLE_BIN`.
- Never delete the legacy directory if any original failed to migrate; leave it and report.

Idempotency: re-running skips originals whose reconstructed `original_path` already has a
meta.json in `.ares-trash` (so a half-finished run resumes cleanly).

## Part B — Photos "Recycle Bin" section

### Backend (app.py)

- `GET /api/photos/trash/list` — returns `list_trash()` filtered to image/video
  extensions, shaped for the gallery: `{trash_name, original_path, filename, trashed_at,
  age_str, purge_in, thumb_url}`. `thumb_url` points at the item's thumb (below).
- `GET /api/photos/trash/thumb/<trash_name>` — serves `.ares-trash/_thumbs/<trash_name>.jpg`
  if present; else generates a thumbnail on demand from the trashed original (reuse the
  existing cold-path thumbnail generator, pointed at the trash file), caches it under
  `.ares-trash/_thumbs/`, and serves it. `require_auth`.
- `POST /api/photos/trash/restore` — `{trash_name}` → existing `restore(trash_name)`.
  On success also remove `.ares-trash/_thumbs/<trash_name>.jpg`.
- `POST /api/photos/trash/purge` — `{trash_name}` → permanently delete the trashed file,
  its meta.json, and its trash thumb. `require_auth`.

`restore()` already refuses to overwrite an existing original and recreates parent dirs —
no change needed there.

### Frontend (Photos gallery)

- A "Recycle Bin" entry point in the Photos view (tab or header button) that loads
  `/api/photos/trash/list` and renders a grid using `thumb_url`, consistent with the
  existing gallery grid styling.
- Each item: click to enlarge; actions **Restore** and **Delete forever** (purge, with a
  confirm). Show `age_str` / `purge_in` per item.
- After restore/purge, refresh the grid and bust the main photo caches (the restore path
  already puts the file back where the scanner will pick it up on next index).

### Ongoing (future deletes get thumbs too)

When a photo is trashed via the existing `/api/photos/trash` route, also copy its current
live thumb (`static/thumbs/<hash>.jpg`) to `.ares-trash/_thumbs/<trash_name>.jpg` so the
review grid has an instant thumbnail without regeneration. If the live thumb is absent,
the on-demand path in the thumb route covers it.

## Non-goals / YAGNI

- No auto-purge timer changes; the existing 30-day `purge_in` field is displayed only.
- No unified "all file types" trash browser — this section is photos/videos only.
- No change to the live-gallery thumbnail scheme or `static/thumbs*`.
- No re-indexing of trashed items into the main photo index (they stay out of the gallery
  proper until restored).

## Risks

- **Thumb-hash mapping** relies on `md5(subpath)`; verified on a sample. The migration
  treats a missing/mismatched thumb as non-fatal (on-demand fallback), so a wrong guess
  degrades to slower first-view, never to data loss.
- **Trash-name collisions** across the two eras handled by uniqueness suffix.
- **Delete-after-verify** gate ensures the legacy directory is removed only once every
  original has a verified new home.
