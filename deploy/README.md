# Dashboard deploy — CRONOS instance

This is the **same codebase** as the ARES dashboard, running on the sister box.
One codebase, two deployments, branded per box via `HOST_BRAND`.

## Naming (they disagree on purpose — here's the map)
| Thing | Value |
|---|---|
| Box hostname | `cronos` (was HERMES) |
| Tailscale IP | `100.100.29.36` |
| Dashboard brand | `CRONOS` (set by `HOST_BRAND` in the systemd unit) |
| Deploy dir | `/home/zain/ARES-DASHBOARD` (yes, "ARES" — it's the shared repo) |
| This dashboard | **port 8890**, Tailscale-only bind |
| The OLD dashboard | `hermes-dash.service` on **port 8888** (still running; decommission when ready) |

## The service (runs WITHOUT sudo — it's a user service)
```
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user status  cronos-dashboard
systemctl --user restart cronos-dashboard
journalctl --user -u cronos-dashboard -n 50 --no-pager
```
Unit file: `~/.config/systemd/user/cronos-dashboard.service`
Survives reboot via **linger** (already enabled: `loginctl show-user zain | grep Linger`).
Env (brand, Tailscale bind, password, empty GPU/PVE hosts) lives in that unit's
`Environment=` lines.

## Health / drift
`curl -s http://100.100.29.36:8890/healthz` → `{ok, brand, caps, stamp}`.
`stamp` is the git SHA of the code this box is running (written on each deploy).

## Redeploy (from ARES)
```
/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/deploy/sync-to-cronos.sh --restart
```
Pushes code (heavy excludes enforced), stamps the box, restarts, and asserts
`/healthz` reports the pushed SHA. Never rsync the repo by hand — use this script.
