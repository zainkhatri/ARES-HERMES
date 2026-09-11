# File Explorer — Operation Module (ARES + ZEUS/HERMES)

**Date:** 2026-08-18
**Status:** Design approved (via LLM Council review), pending spec sign-off → implementation plan.

## Goal

A read-only file explorer operation module, in theme with the rest of the dashboard, on both ARES (red) and ZEUS/HERMES (blue). Browse the box's PROMETHEUS storage pool from a browser (including mobile), open/preview documents and media, and download files. Each box browses **only its own** filesystem.

## Scope (locked)

- **Read-only.** No rename, delete, move, upload, or mkdir. Ever.
- **Confined** to each box's PROMETHEUS storage root (`POOL_ROOT` env; `/mnt/nvme/PROMETHEUS` on ARES, `/srv/mergerfs/PROMETHEUS` on HERMES).
- **Vault excluded** — the My Eyes Only vault (`PHOTOS/.vault`, `vault_enc/`, `vault_thumbs*`, `vault_video_cache`, `vault_hls`) must never be listed or served. Hard rule.
- **Own box only.** Two independent, mirrored modules. No cross-box browsing/proxying in v1.
- **Current-folder filter only.** No recursive search (a prior `/srv`-wide grep wedged HERMES to load 40).

### Explicitly OUT of scope (v1) — cut by council as attack-surface creep / YAGNI
CLIP "find similar", folder vector search, AI/Claude ingestion, cross-box unified drive, any write operation. Revisit later as separate specs if the explorer proves useful.

## Architecture

New focused module **`system/files_api.py`** registered into `app.py` (keeps logic out of the 9k-line monolith; makes the HERMES mirror a clean file drop). Three routes, all behind the existing `require_auth` decorator:

- `GET /files` → renders themed HUD page `templates/files.html`.
- `GET /api/files/list?path=<rel>` → JSON directory listing (one `os.scandir`, no recursion).
- `GET /api/files/raw?path=<rel>&dl=0|1` → streams one file (inline preview or attachment download).

`/files` and `/api/files/*` are **not** under any Caddy direct-serve location, so Flask handles them and its security headers apply (Caddy serves `/static`, `/thumbs*` directly — the explorer must not route raw bytes through those paths, to avoid the Flask-vs-Caddy resolver disagreement the council flagged).

### The security core — `_safe_resolve(rel) -> abspath | None`

Single chokepoint. Every filesystem access goes through it. Order matters:

```
def _safe_resolve(rel):
    # 1. reject absolute paths and NUL bytes up front
    # 2. abspath = os.path.realpath(os.path.join(ROOT, rel))   # resolves .. AND symlinks
    # 3. confinement: abspath == ROOT or abspath.startswith(ROOT + os.sep)  else None
    # 4. vault/deny check on the RESOLVED path's components (not the requested string):
    #       any component in DENY_NAMES  -> None
    #       any component startswith "." (dotfile/dot-dir) -> None   # hides .vault, .claude, etc.
    # 5. return abspath
```

Key decisions, each from a specific council finding:

- **Vault/deny checked on the realpath, not the request string.** Defeats a symlink that points *into* the vault whose request string contains no vault segment. (Contrarian + First-Principles.)
- **Realpath-based confinement (not "forbid all symlinks").** The pool contains a legitimate in-pool symlink (`NEXUS-SIDEKICK → .../HERMES-SIDEKICK`); realpath keeps it browsable while still rejecting any symlink that resolves outside ROOT or into the vault. Fail closed on escape.
- **Dotfiles hidden by default.** `.vault` is a dot-dir, so this alone hides it; `DENY_NAMES` is belt-and-suspenders for the non-dot vault dirs (`vault_enc`, `vault_thumbs`, `vault_video_cache`, `vault_hls`, `My Eyes Only`).
- **ROOT is derived from `POOL_ROOT` env at import, with a startup assert that it exists and is a directory.** The HERMES copy re-derives its own ROOT — it must never inherit ARES's hardcoded `/mnt/nvme/...`. (Peer-review: HERMES drift.)

### `/api/files/list` behavior

- One `os.scandir(dir)`; entries: `{name, is_dir, size, mtime, kind}`. `kind` from extension: `dir|pdf|image|video|audio|text|md|file`.
- Dotfiles and `DENY_NAMES` filtered out.
- **Listing cap: 2,000 entries** + a `truncated: true` flag so a 50k-file dir can't jank a phone or stall the worker. (Executor + Outsider.)
- Sorting: dirs-first, then name (case-insensitive). Client can re-sort by name/date/size without a server round-trip.
- Returns `{cwd, parent, entries, truncated}`. `parent` is null at ROOT.

### `/api/files/raw` behavior — hardened serving

- Resolve via `_safe_resolve`; open the **resolved realpath** with `os.open(..., O_RDONLY | O_NOFOLLOW)`, `fstat` to confirm it is a **regular file** (reject dirs/devices/fifos), then stream from that fd. This closes the TOCTOU window (a symlink swapped in during the race makes `O_NOFOLLOW` fail → reject). (Contrarian.)
- **Attachment by default.** `Content-Disposition: attachment` for everything EXCEPT a strict inline allowlist: `image/*`, `video/*`, `audio/*`, `application/pdf`. Text/markdown served as `text/plain; charset=utf-8` (never `text/html`, never `image/svg+xml` inline). Prevents stored-XSS against the authed same-origin session. (Contrarian.)
- Always set `X-Content-Type-Options: nosniff` and `Content-Security-Policy: sandbox` on this endpoint.
- Byte-range enabled (`send_file(conditional=True)` semantics, or manual range on the fd) so video/audio/PDF stream and scrub on mobile instead of full-download.
- **Known ceiling (accepted for v1):** one gunicorn worker means a large blocking stream stalls other requests. `# ponytail: single-worker stream stall; upgrade path = Caddy X-Accel-Redirect if it bites.`

### Error semantics

- **Identical `404` for both not-found and forbidden/vault paths** (and constant-ish handling) so the vault's existence is never confirmable via status code. (Peer-review: existence leakage.)
- No stack traces or raw exception text in responses.

## Frontend — `templates/files.html`

HUD-themed (reuses `_hud_head.html`, `_hud_nav.html`, the `.op`/panel styling, `--ares`/ZEUS palette via `data-brand`). Client JS (inline or `static/files.js`):

- **Breadcrumb** path with a clear "up"/root affordance (mobile-friendly).
- **List view**: type icon, name, size, mtime. Folders open in place (fetch `list`); files open the preview pane.
- **Filter box** labeled "Filter this folder" (so it's obviously current-folder-only, not broken recursive search). Client-side, instant.
- **Sort** toggle: name / date / size.
- **Preview pane**: image `<img>`, video/audio native players, pdf `<iframe>`, text/md fetched-and-shown in `<pre>` (light markdown render optional). Every file also shows a **Download** button.
- **Graceful fallback**: non-previewable types (`.heic`, `.mov`, `.zip`, unknown) show metadata + Download, never a broken viewer. (Outsider.)
- `truncated` flag surfaces a "showing first 2,000 — narrow with filter" notice.

## Home page wiring

Add a fifth operation module to `templates/home.html` ops nav, matching the existing `.op` pattern:

```
<a href="/files" class="op"><span class="op-num">005</span>
  <span class="op-body"><span class="op-lab">Storage / Explorer</span><span class="op-name">Files</span></span>
  <svg class="op-arrow" .../></a>
```
Bump the `Operation modules` count `04 active` → `05 active`.

## Mirroring to HERMES/ZEUS

HERMES-DASH is a separate twin codebase (`/srv/mergerfs/PROMETHEUS/HERMES-DASH`, :8888, runs as `zain`). Apply the same changes there via scp-edit:
- Copy `system/files_api.py` verbatim (ROOT comes from env, so no edit needed — assert catches a bad env).
- Add the same route registration, `files.html`, and home-nav op item.
- Verify ROOT resolves to `/srv/mergerfs/PROMETHEUS` and the module runs correctly as non-root (permission edge cases differ from ARES-as-root).
- Do **not** refactor into a shared package — two copies, one is a literal copy.

## Testing

`test_files.py` — the one test that must exist (unanimous council): `_safe_resolve` rejects, **after realpath**:
- `../` and `../../etc/passwd` traversal,
- absolute paths,
- a symlink inside ROOT pointing outside ROOT (fixture),
- a symlink inside ROOT pointing into the vault (fixture),
- any dotfile / `DENY_NAMES` component,
and accepts a normal in-ROOT path and a legit in-ROOT symlink. Assert forbidden and not-found both surface as 404 at the route layer. No framework beyond `assert` + a `__main__`/`test_*` runner.

## Build order (Executor)

1. `_safe_resolve` + `test_files.py` — before any route.
2. Three routes in `system/files_api.py`, register in `app.py` (watcher auto-restarts).
3. Verify `list` + `raw` (inline + download + range) in the browser against real dirs.
4. Build `files.html` (the fiddly part — 6 preview types) + home-nav op item.
5. `scp` module/template/nav to HERMES-DASH, verify ROOT + non-root behavior, restart.

## Open risks accepted for v1
- Single-worker stream stall (ceiling noted above).
- No audit log of root-level reads (add later if needed).
- No rate-limiting on raw (single authed user; low risk).
