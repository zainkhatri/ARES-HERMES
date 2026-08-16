# Rename the mini PC → ZEUS — design

**Date:** 2026-08-16
**Goal:** The mini PC (currently branded CRONOS, hostname `cronos`, Tailscale device
`hermes`) is renamed **ZEUS** everywhere it means *the mini PC*.

## Scope (confirmed with user)
**In:** the mini PC's *identity* — `CRONOS`, `NEXUS` (old brand), and the mini-PC
`HERMES` references (hostname, Tailscale device, `/jump/hermes` routes).
**Out (explicitly kept):**
- `/PROMETHEUS/` — the storage-pool ROOT on BOTH boxes. Not the mini PC. Renaming =
  filesystem migration that breaks everything. Untouched.
- `PROMETHEON` — the iOS app. Separate. Untouched.
- `ARES-DASHBOARD` repo/dir name (shared codebase), `Mid-NAS`/`Super-NAS` role tags.

## Blast radius (surveyed)
- ARES code: ~10 files reference CRONOS/hermes (system_info.py, app.py, home.html,
  login.html, node-switcher.js, pty_ws.py, dupes_review/drives/shell/journals.html).
  `/jump/hermes` SSO route + handler. The CRONOS wordmark ASCII art.
- Mini PC: `HOST_BRAND=CRONOS`, user service `cronos-dashboard`, OS hostname `cronos`,
  Tailscale device `hermes`.
- **FAI funnel:** NOTHING on the box hardcodes `hermes.tail…net` (verified — no
  container env, no compose). The rename is internally clean. EXTERNAL consumers of
  `hermes.tail…net` (webhooks, bookmarks) must be repointed to `zeus.tail…net` by the
  user — invisible from the box.

## Changes
| Where | From → To |
|---|---|
| `system_info.py` caps key | `"CRONOS"` → `"ZEUS"` |
| home.html wordmark | CRONOS ASCII → **ZEUS** ASCII (ANSI Shadow, red `ZE`/white `US`) |
| home.html/login.html/node-switcher.js | brand `CRONOS`→`ZEUS`, `MID-NAS` tag kept |
| Jinja `== 'CRONOS'`, `is_cronos` | → `'ZEUS'`, `is_zeus` |
| `/jump/hermes` route + `jump_hermes()` + all links | → `/jump/zeus`, `jump_zeus()` |
| CSS `node-cronos`, `t-hermes` | → `node-zeus`, `t-zeus` |
| ARES `.env` | `PEER_NAME=CRONOS` → `ZEUS` |
| deploy `sync-to-cronos.sh`, `cronos-dashboard.service` | → `sync-to-zeus.sh`, `zeus-dashboard.service` |
| mini PC unit | `HOST_BRAND=ZEUS`; service `cronos-dashboard`→`zeus-dashboard` |
| mini PC OS hostname | `cronos` → `zeus` (needs sudo → user step) |
| Tailscale device | `hermes` → `zeus` (moves `hermes.tail…net`→`zeus.tail…net`) |

## Sequence (safe order)
1. ARES code rename + ZEUS wordmark → verify (node --check, render) → commit.
2. Rename deploy script/unit → deploy to mini PC.
3. Mini PC: flip `HOST_BRAND=ZEUS`, rename user service, restart, verify.
4. **Last:** Tailscale device rename (moves the public URL). Verify funnel + dashboard.
5. OS hostname (sudo) — hand to user if passwordless sudo unavailable.

## Reversibility
Code is git-reverted. Service/unit rename keeps old unit disabled (re-enablable).
Tailscale device rename is reversible (`tailscale set --hostname=hermes`). Storage
untouched, so no data risk.
