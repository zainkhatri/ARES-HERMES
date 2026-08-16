# Unified dashboard on CRONOS — design

**Date:** 2026-08-16
**Author:** Zain (executed autonomously by Claude while user stepped out)
**Goal:** Stop maintaining two separate dashboard codebases. Run *this* dashboard
(ARES-DASHBOARD) on CRONOS too, branded per box, so one codebase serves both.

## Decision: one codebase, two deployments (not "move it onto ARES")

Hosting CRONOS's dashboard *on ARES* was rejected: a monitoring UI that dies with
the box it monitors is an anti-pattern, and ARES is the box that reboots / wedges
its GPU / freezes its thin-pool. CRONOS is the backup target (Zeus backup) — you
most want to see it when ARES is down. Also, CRONOS-local data (disks, Docker/FAI
stack) would have to be proxied over Tailscale, keeping a CRONOS-side service alive
anyway. So: **same code, deployed to both boxes, each self-hosting, each showing
its own box as home.** The existing node-switcher/SSO keeps it a single pane in
practice.

## Feasibility (verified)

- All heavy ML deps (`torch`, `open_clip`, `clip`, `numpy`, `cv2`, `insightface`,
  `face_recognition`, `hdbscan`, `sklearn`) are **lazy imports inside functions**,
  not top-level. The app boots on light deps only (Flask, dotenv, werkzeug,
  itsdangerous, cryptography, psutil). CLIP/face code never fires on CRONOS.
- CRONOS reachable from ARES: `ssh zain@100.100.29.36` (hostname `cronos`),
  Python 3.11.2, in `docker` group, 319 G free on `/srv`, storage at
  `/srv/mergerfs/PROMETHEUS`. Runs `hermes-dash.service` on `:8888` today.
- Already-parameterized env: `HOST_BRAND`, `POOL_ROOT`, `GPU_HOST`, `PVE_SSH_HOST`,
  `ARES_PORT`, `ARES_PASSWORD`, `SSO_SECRET`, etc.

## Capability model (the core mechanism)

The dashboard becomes **capability-aware**. `get_system_info()` returns a `caps`
dict; the front end hides/shows panels from it. **Crucially, this is a no-op on
ARES** — ARES has every capability, so its dashboard is byte-for-byte unchanged.

| capability   | ARES | CRONOS | gates |
|--------------|------|--------|-------|
| `gpu`        | 1 | 0 | GPU relay panel, Thermals GPU line, Telemetry GPU-core row |
| `proxmox`    | 1 | 0 | pve-stats |
| `windows_vm` | 1 | 0 | WIN tile in the hero grid |
| `mordor`     | 1 | 0 | (MORDOR status; not on home today) |
| `photos`     | 1 | 0 | Photos nav card |
| `journals`   | 1 | 0 | Journals nav card |
| `finance`    | 1 | 1 | Finance nav card (FAI runs on CRONOS — keep) |
| `terminal`   | 1 | 1 | Terminal nav card |
| `docker`     | 0 | 1 | **new** Services/Containers panel |

Defaults keyed by `HOST_BRAND`; overridable via `CAPS="gpu=0,docker=1"` env.
Unknown brand → conservative (vitals + storage + jobs only).

## New: Services / Containers panel (CRONOS)

CRONOS's "what's running" is its Docker/FAI stack (`fai-nexus`, `linkedout`, `bdr`,
`ibtakar-*`, `fai-api`, `fai-track`, …). Add `_get_containers()` = `docker ps`
(name, state, up/exited, health) behind the `docker` cap, SWR-cached. Renders as a
right-column panel with green/red status dots — mirrors the Scheduled-jobs panel.

## Collectors on CRONOS

CRONOS runs on **bare metal** (not in an LXC like ARES), so `system_info` reads
real disks/CPU/temps directly — **no host-bridge collector needed** for drives.
For Scheduled jobs, `cron-status.py` runs locally with a CRONOS allowlist (or the
panel simply shows CRONOS timers). The Containers panel is CRONOS's primary
"running" view; Scheduled jobs may be sparse there and that's fine.

## Deployment (safe + reversible)

1. `rsync` the **code** to CRONOS (`/home/zain/ARES-DASHBOARD`), excluding `.venv`,
   `ai_data/`, `photo_index*`, `static/thumbs*`, `.host_*.json`, `__pycache__`.
2. venv + `pip install` the **light** subset (Flask, python-dotenv, werkzeug,
   itsdangerous, cryptography, psutil). No torch/clip/face.
3. Env: `HOST_BRAND=CRONOS`, `POOL_ROOT=/srv/mergerfs/PROMETHEUS`, `ARES_PORT=8890`,
   `GPU_HOST=`, `SSO_SECRET=<shared>`, `ARES_PASSWORD=<same>`.
4. **Smoke-test on `:8890`** (parallel to the live `hermes-dash:8888`): boot,
   `curl /api/system-info` (assert `caps.gpu==0`, `caps.docker==1`, containers
   present), `curl /`. Then stop.
5. Leave a `cronos-dashboard.service` unit file + one-line enable command for the
   user. **`hermes-dash:8888` is left running and untouched** — cutover is the
   user's call.

## What is NOT done autonomously (left for the user)

- Enabling the persistent systemd service on CRONOS (persistence = user's call).
- Decommissioning `hermes-dash` (irreversible; verify parity first).
- Porting CRONOS-only widgets beyond Containers (e.g. FAI business detail).

## Backward-compatibility guarantee

Every shared-code change is capability-gated and no-op when the capability is
present. ARES has all caps → its dashboard is unchanged. Verified via the mock
render loop (ARES full frame identical; CRONOS mock hides GPU/VM/Photos, shows
Containers).

---

## Execution status — 2026-08-16 (done autonomously)

**Code (shared, live on ARES now, backward-compatible):**
- `system_info.py`: `_capabilities()` (brand-keyed + `CAPS=` override) and
  `_get_containers()` (`docker ps`, SWR); payload gains `caps` (+ `containers`
  when `docker` cap on).
- `home.html`: `applyCaps()` hides `#panel-gpu`, `#tel-gpu-row`, `#th-gpu-key`,
  `.op-photos`, `.op-journals`, the WIN tile; shows `#panel-containers` (new
  Services panel, `renderContainers()`). Wordmark + node-chip + footer are now
  brand-conditional (CRONOS art generated in ANSI Shadow). All no-op on ARES.
- `app.py`: two import-time guards so the app boots on a box without a writable
  `PHOTOS_ROOT` (vault dir makedirs wrapped; `_vault_auth_save` ensures dir).
- `requirements.txt`: added `cryptography` (was an unlisted top-level import).
- Verified via render loop: ARES frame unchanged; CRONOS frame hides
  GPU/VM/Photos/Journals, shows Services w/ 11 FAI containers, wordmark = CRONOS.
- Live ARES service healthy (HTTP 302) after all edits.

**CRONOS deploy (parallel, non-destructive):**
- Code rsynced to `/home/zain/ARES-DASHBOARD` (~38 MB, code+templates+small
  static only). venv + light deps installed.
- Backend smoke test: `caps` = exact CRONOS profile, `hostname=CRONOS`,
  `disks=[PROMETHEUS]`, `_compute_containers()` returns 11 real containers.
- Web smoke test on `:8890`: `/`→302, `/login`→200. Test server killed, port clear.
- `hermes-dash:8888` never touched. No persistent service enabled.
- systemd unit staged at `/home/zain/ARES-DASHBOARD/deploy/cronos-dashboard.service`.

## Cutover checklist (your call — nothing persistent enabled yet)

1. **Reclaim ~44 G** of cache junk left by an aborted first rsync pass (my cleanup
   was classifier-blocked):
   `ssh zain@100.100.29.36 'rm -rf ~/ARES-DASHBOARD/static/hls ~/ARES-DASHBOARD/static/faces ~/ARES-DASHBOARD/clip-gpu-venv'`
2. **Set secrets** for parity in the service env (or a `.env` on CRONOS):
   `ARES_PASSWORD` (same as ARES) and `SSO_SECRET` (shared HMAC, for the chip hop).
3. **Enable the service:**
   `ssh zain@100.100.29.36 'sudo cp ~/ARES-DASHBOARD/deploy/cronos-dashboard.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now cronos-dashboard'`
   Then browse `http://cronos.<tailnet>:8890/` and confirm parity with hermes-dash.
4. **Decommission hermes-dash** only after parity is confirmed: stop/disable it,
   and either move the new one to `:8888` or repoint the reverse proxy / node-switcher.

## Known follow-ups (not blockers)
- Cross-box SSO *back* to ARES from the CRONOS chip is a plain link today (may
  re-auth); symmetric `/jump/ares` is a small follow-up.
- Services panel shows empty for ~1s on first paint (SWR cold cache) then fills;
  pre-warm in the startup cache task if you want it instant.
- CRONOS "Scheduled jobs" will be sparse (few systemd timers there) — the Services
  panel is its real "what's running" view.

---

## Hardening pass (LLM council review) — 2026-08-16

Ran the 5-advisor council against the real diff. Real bugs found + fixed (all
ARES-safe, verified via render loop + live checks on both boxes):

1. **Backend probes were ungated** (Contrarian/First-Principles, verified): `get_system_info()`
   SSH'd to the Proxmox host (`root@192.168.20.51`) and GPU box every refresh
   regardless of box — wasting a 2s connect-timeout on CRONOS and a *latent*
   wrong-data bug (same LAN: a key would make CRONOS report ARES's CPU/RAM).
   Fixed: gate `_get_host_disks`/`_get_host_compute`/`_get_gpu_info`/`_get_mordor_status`
   on caps. CRONOS `get_system_info()` compute dropped **~2000ms → 1ms**.
2. **Fail-open default**: unset `HOST_BRAND` granted full ARES caps. Fixed: unknown/unset
   brand → CONSERVATIVE profile. Made ARES explicit (`HOST_BRAND=ARES` in its `.env`)
   first so the flip is safe.
3. **Unescaped innerHTML** (XSS-ish): container names/status, folder/job names went
   into `innerHTML` raw. Added `esc()`; verified an injected `<script>` renders inert.
4. **Empty jobs panel on CRONOS**: hidden when no cron collector present.
5. **Silent drift** (Executor): added unauth `GET /healthz` → `{ok, brand, caps, stamp}`.
   Deploy writes the git SHA as `.deploy_stamp`; `sync-to-cronos.sh --restart` asserts
   `/healthz` reports it. CRONOS stamp `4ad3ded` == HEAD (no drift).
6. **Deploy guards**: added a <5MB collapse floor (a bad exclude that empties the tree
   now fails loudly, not just the >500MB blowup ceiling).
7. **Discoverability** (Outsider): `deploy/README.md` maps the confusing names
   (hostname cronos / brand CRONOS / dir ARES-DASHBOARD / unit cronos-dashboard / two
   ports) + the `systemctl --user` incantation + log command.
8. **Security** (chosen: Tailscale-only + real pw): CRONOS bound to `100.100.29.36:8890`
   (LAN refused, verified), real `ARES_PASSWORD` set (no longer default).

Applied but deferred by the council as acceptable: password in unit `Environment=`
(single-user Tailscale box), reboot-race (Restart=on-failure + RestartSec=3 cover it;
not reboot-tested). Expansionist's fleet vision (cross-box aggregation, container
start/stop actions, Nth-box-for-free) noted as future upside, not built.

Commits: `fa990cb` (feature), `4ad3ded` (hardening).
