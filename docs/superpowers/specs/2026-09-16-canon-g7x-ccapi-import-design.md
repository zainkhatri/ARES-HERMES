# Canon G7X Mark III → ARES auto-import (CCAPI) — Design

Date: 2026-09-16
Status: Approved design, pending implementation plan

## Goal

Replace the Canon Camera Connect app workflow. The user presses the WiFi button
on a Canon PowerShot G7X Mark III. New photos transfer over WiFi into the ARES
`PHOTOS/` library, appear in the gallery within seconds, and ARES shows a live
"photos inbound" animation while the transfer runs. No SD-card reader, no phone,
no Canon cloud.

## Facts that constrain the design

- The G7X Mark III supports **CCAPI** (Canon Camera Control API): an official
  REST-over-HTTP API. It must be activated once with Canon's activation tool
  (free developer-community registration). After activation it is plain HTTP.
- `gphoto2` over WiFi is broken for the G7X series (documented I/O errors). Not
  used.
- Canon's native "auto send to computer" needs *Image Transfer Utility 2*, which
  is Windows/Mac only. Not used (keeps the Mac out of the loop).
- The gallery ingestion seam already exists: a file dropped into
  `/mnt/nvme/PROMETHEUS/PHOTOS/<YYYY>/<MM>/` becomes a gallery entry after
  `photo_scanner.py --incremental` runs. Thumbnails generate on-demand
  (`app.py:_serve_thumb_on_demand`). A cron runs the incremental scan every
  15 min today; this design triggers it immediately after each drain.
- `photo_db.save_items` has a >50% shrink guard. This design only ADDS files, so
  the guard is never at risk.

## Decisions

| Decision | Choice |
|---|---|
| Transfer path | CCAPI direct, camera → ARES host (no Mac, no cloud) |
| Where it runs | Host systemd service (PHOTOS + photo_scanner live on host) |
| File types | JPEG only (RAW/CRAW stays on the card) |
| Camera discovery | Fixed IP via router DHCP reservation |
| Reindex | Immediate `photo_scanner.py --incremental` after each drain |
| SD card | Non-destructive — never delete from the card |
| Transfer animation | Corner toast ("photos inbound", live counter) — v1 |
| Persistent status tile | Phase 2 (out of scope for v1) |

## Architecture

```
Canon G7X III  ──WiFi (CCAPI / HTTP)──►  ares-camera-import (host poll loop)
  press WiFi button                          1. fast TCP probe camera IP (idle ~10s)
  → joins home WiFi                          2. list contents, diff vs ledger
  → CCAPI HTTP server up                     3. download new JPEGs → PHOTOS/YYYY/MM/
                                             4. mark ledger (non-destructive)
                                             5. write status file (drives animation)
                                             6. photo_scanner.py --incremental
                                                        │
                                                photo_index.db → gallery
```

### The "one button" experience

1. User presses the camera WiFi button → camera joins the home WiFi.
2. The host service is a standing idle-poller. Every ~10s it does a fast,
   short-timeout TCP probe to the camera's reserved IP. An unreachable camera
   fails fast and cheap.
3. When the camera answers, the service **drains all new photos in one pass**
   (camera WiFi sleeps quickly, so grab everything immediately), reindexes, then
   returns to idle polling.

## Components

Each unit has one purpose, a defined interface, and is testable in isolation.
Target: files under 500 lines; functions ~60 lines; bounded loops; timeouts on
every network call; validate every response and return value (Power-of-Ten
spirit).

### `camera/ccapi_client.py`
Thin CCAPI HTTP wrapper. No orchestration, no filesystem writes.
- `ping() -> bool` — camera reachable + CCAPI up (short timeout).
- `api_versions() -> dict` — `GET /ccapi/`; used to pin the endpoint version
  (ver100/ver110) at runtime rather than hard-coding.
- `list_contents() -> list[ContentRef]` — enumerate storage/folders/files.
- `file_metadata(ref) -> dict` — name, size, capture time, type.
- `download(ref, dest_path, kind="main")` — stream full-res to a temp path,
  then atomic rename into place.
- Every method: explicit timeout, status-code check, typed return or raise.

### `camera/ledger.py`
Dedup ledger so nothing is pulled twice (SD card is never modified).
- Storage: sqlite at `ai_data/camera_import.db`.
- `is_pulled(content_id) -> bool`
- `mark_pulled(content_id, dest_path, size, ts)`
- A pull only counts as done AFTER the file is verified on disk (size matches
  CCAPI metadata) — a mid-transfer drop is retried next cycle.

### `camera/importer.py`
Orchestration and the poll loop.
- `poll_loop()` — bounded per-cycle work, sleep between cycles, no hot-spin.
- `drain()` — list → diff vs ledger → for each new JPEG: resolve capture date →
  build `PHOTOS/<YYYY>/<MM>/<name>` (collision-safe suffix) → `download` →
  verify → `mark_pulled` → update status counter. Per-file try/except so one bad
  file cannot stall the batch.
- `_reindex()` — run `photo_scanner.py --incremental` (subprocess) after a drain.
- `_write_status(...)` — update the status file (see below) at each state change
  and per-file during a drain.

### `camera/status.py`
Single source of truth for the animation state.
- Writes `ai_data/camera_import_status.json` atomically:
  ```json
  {
    "state": "idle | connected | draining | done | error",
    "batch_total": 27,
    "batch_done": 12,
    "pulled_today": 41,
    "last_update": 1789999999.0,
    "message": "Added 27 photos"
  }
  ```
- The repo is bind-mounted into LXC 101, so Flask reads this file directly — the
  same filesystem-IPC pattern the business dashboard already uses. No new socket
  or cross-host call.

### systemd unit `ares-camera-import.service` (host)
- Runs `python3 -m camera.importer` as a host service alongside ttyd /
  ares-shell-ctl.
- `Restart=always`. Config via env (see below).

### Flask: `GET /api/camera/status`
- New route in `app.py`, wrapped in `require_auth`.
- Reads `ai_data/camera_import_status.json`, returns it as JSON. Returns a safe
  `idle` default if the file is missing.

### Frontend: corner toast (in `templates/home.html` + `static/hud.css`)
- Home page polls `/api/camera/status` every ~2s (cheap; matches the existing
  3s-poll perf pattern — NO continuous rAF loop, per the idle-repaint-lag fix).
- On `state == "draining"`: a compact toast slides in bottom-right — "📷 PHOTOS
  INBOUND", a segmented meter bar, and a live `batch_done / batch_total` counter.
- On `state == "done"`: brief flourish "Added N photos", then fade out.
- On `state == "error"`: red toast with the message.
- Styling matches house style: sharp tiles, accent red `#ef4444`, Bricolage
  numerals, segmented meter bars. Study `templates/home.html` before building.
- CSS-triggered animation only (keyframes fire on class change); bump `hud.css`
  `?v=` mtime cache-bust after edits.

## Configuration (env, read by the service)

| Var | Default | Purpose |
|---|---|---|
| `CAMERA_IP` | — | Reserved IP of the G7X III |
| `CAMERA_CCAPI_PORT` | `8080` | CCAPI HTTP port (pinned at impl time) |
| `CAMERA_POLL_INTERVAL` | `10` | Idle probe interval (seconds) |
| `CAMERA_DEST_ROOT` | `/mnt/nvme/PROMETHEUS/PHOTOS` | Import target root |
| `CAMERA_PULL_RAW` | `0` | JPEG only by default |
| `CAMERA_PROBE_TIMEOUT` | `2` | Fast-fail probe timeout (seconds) |

## Error handling & safety

- **Non-destructive**: the SD card is never modified; the ledger prevents
  re-pulls.
- Every HTTP call has a hard timeout; downloads stream to a temp file and are
  size-verified before the atomic rename and ledger write.
- Bounded per-file retry; the idle loop always sleeps between cycles.
- Filename collisions get a numeric suffix; capture date resolves from CCAPI
  metadata/EXIF, falling back to the file mtime, then to "unknown/" so nothing is
  ever silently dropped.
- Reuses the existing `photo_scanner.py` shrink guard (add-only path).

## One-time setup (documented for the user, not code)

1. Update camera firmware; run Canon's **CCAPI activation tool** once (free
   dev-community registration).
2. Set the camera WiFi to **join the home network** (station mode) and assign
   that connection to the WiFi button.
3. Add a **DHCP reservation** for the camera; set `CAMERA_IP` to it.

## Testing

- `ccapi_client`: unit tests against a mock HTTP server returning recorded CCAPI
  responses (versions, contents list, file metadata, a small file body). Cover
  timeout, non-200, and truncated-download cases.
- `ledger`: insert / is_pulled / dedup / verify-before-mark.
- `importer.drain`: with a fake client + temp dest — asserts correct date
  foldering, collision handling, per-file failure isolation, ledger writes, and
  status-file transitions.
- `status`: atomic write + schema; Flask route returns the file / safe default.
- Manual end-to-end: press the button, confirm the toast animates, confirm the
  photos land in `PHOTOS/<YYYY>/<MM>/` and appear in the gallery.

## Out of scope (phase 2)

- Persistent "Camera" status tile on the dashboard.
- RAW/CRAW import.
- Optional post-transfer "delete from card" mode.
- Push notification on transfer complete.
