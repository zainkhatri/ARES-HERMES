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

## Clean URL (tailscale serve)
The dashboard has a tailnet-private HTTPS URL (works from any device with
Tailscale + MagicDNS — Mac/phone/etc; NOT public, NOT Funnel):

    https://hermes.tail3045df.ts.net:8443/

Set up (additive — does NOT touch the public :443 Funnel that serves the FAI stack):

    tailscale serve --bg --https=8443 http://100.100.29.36:8890   # persists across reboot
    tailscale serve --https=8443 off                              # to remove

Note: the box's DNS name is `hermes` in Tailscale (renaming to `cronos` would move
the FAI Funnel URL too, so it's left as-is). A self-`curl` from this box resolves the
name to the public Funnel IPs and won't reach :8443 — that's expected; MagicDNS
clients (your devices) resolve it to the 100.100.29.36 tailscale IP and reach it fine.
Direct `http://100.100.29.36:8890/` also still works.


## Sister-node card (peer health at a glance)
Each box's home dashboard shows a compact card with the OTHER box's live health
(● up/down, cpu %, mem %, containers up/down, failed jobs) — tap it to open the
peer's dashboard. Config per box (ARES `.env`, CRONOS systemd unit):

    PEER_URL   = http://<peer tailscale IP>:<port>   # ARES->CRONOS :8890, CRONOS->ARES :8080
    PEER_NAME  = CRONOS | ARES
    PEER_TAG   = Mid-NAS | Super-NAS

The card fetches the peer's `/healthz` **from the browser** (client-side), because
ARES's Flask runs in an LXC with no Tailscale route to the peer — your browser is
on the tailnet and reaches both. `/healthz` is CORS-open (non-secret, tailnet-only).
Caveat: load the dashboards over **http** (the IP links) — an https page can't
fetch the http peer (mixed content), and the card would show "unreachable".

## Health / drift
`curl -s http://100.100.29.36:8890/healthz` → `{ok, brand, caps, stamp}`.
`stamp` is the git SHA of the code this box is running (written on each deploy).

## Redeploy (from ARES)
```
/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/deploy/sync-to-cronos.sh --restart
```
Pushes code (heavy excludes enforced), stamps the box, restarts, and asserts
`/healthz` reports the pushed SHA. Never rsync the repo by hand — use this script.
