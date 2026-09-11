# Fleet consolidation — one ARES dashboard, three machines

Date: 2026-09-08
Status: approved, pre-implementation

## Goal

Show ARES, EROS, and ZEUS on the single ARES web dashboard. Eliminate the need
to open separate dashboards per box. ARES already renders its own vitals. Add
EROS (full vitals) and ZEUS (backup info only) as new panels in the existing
HUD grid.

## Constraint that drives the design

The dashboard Flask process runs in LXC 101 (192.168.20.213). The LXC has NO
network route to the peer boxes:

- ZEUS  = 100.100.29.36 (tailnet only)
- EROS  = 10.0.1.69 (janjee LAN, different subnet)

The existing sister-box widget works around this by returning config to the
browser and letting the browser fetch the peer client-side. EROS runs no
dashboard, so browser-fetch is not available for it.

The ARES **host** (root) reaches both peers over SSH — this is how
`/etc/profile.d/prometheus-motd.sh` already probes them.

## Architecture — host collector writes a file, dashboard reads it

This reuses the pattern the codebase already uses for cross-box data:
`/api/outreach` reads a local `snapshot.json`; the MOTD reads
`HERMES-SIDEKICK/status/rollup.json`.

```
ARES host
  fleet-collect.sh   (systemd timer, every 60s)
    - SSH EROS 10.0.1.69: cpu load/%, mem, root disk, temp, `qm list`, ollama
      model count, uptime
    - read local backup status files (same paths the MOTD reads):
        ARES->ZEUS : /var/log/ares-backup-to-hermes.status   ("OK|WARN|FAIL <epoch>")
        ZEUS->ARES : newest dated dir in
                     /mnt/nvme/PROMETHEUS/HERMES-SIDEKICK/snapshots/YYYY-MM-DD
    - ZEUS reachability: ping 100.100.29.36
    - write ai_data/fleet.json  (atomic: write .tmp then rename)

Dashboard (LXC)
  GET /api/fleet  -> read ai_data/fleet.json, add age_sec = now - file mtime,
                     return JSON. {stale:true} when age_sec > 300.
  home.html       -> poll /api/fleet on the existing poll loop; render 2 panels.
```

`ai_data/` is already bind-mounted into the LXC (CLIP embeddings, vault auth
live there), so the file is visible to both host writer and LXC reader with no
new mount.

## fleet.json shape

```json
{
  "ts": 1757280000,
  "eros": {
    "up": true,
    "cpu_pct": 3, "temp_c": 41,
    "mem_used_gb": 4.1, "mem_total_gb": 16.0,
    "disk_used_gb": 35, "disk_total_gb": 94, "disk_pct": 40,
    "uptime": "2d 4h",
    "vms": [ {"id": 200, "name": "win11-gaming", "state": "running"} ],
    "ollama_models": 0
  },
  "backups": {
    "ares_to_zeus": {"state": "OK",   "epoch": 1757270000},
    "zeus_to_ares": {"state": "DATED","epoch": 1757190000},
    "zeus_reachable": true
  }
}
```

Missing / unreachable EROS -> `{"up": false}`; the panel shows offline, no
crash. Any field the probe could not read is omitted and renders as "—".

## Collector detail (EROS single SSH call)

One `ssh root@10.0.1.69` running a small remote script that emits key=value
lines (robust to parse, no jq dependency on either side beyond what the
collector already uses). Fields:

- cpu_pct  : from `/proc/stat` delta OR `top -bn1` load — 1s sample
- temp_c   : `sensors -j` if present, else omit
- mem      : `free -m`
- disk     : `df -P /`
- uptime   : `uptime -p` trimmed
- vms      : `qm list` (id, name, state)
- ollama   : `curl -s -m2 localhost:11434/api/tags | jq '.models|length'`, else 0

Timeouts: `timeout 12 ssh -o BatchMode=yes -o ConnectTimeout=8`. On any
failure the collector still writes fleet.json with `eros.up=false` so the
dashboard never reads a half-written or ancient file.

## UI — two new panels

Both are standard `<section class="mod">` blocks (mod-head title+meta, mod-body).

1. EROS panel — right column, under `#panel-gpu`. id `panel-eros`.
   - meta: online/offline + "updated Ns ago" (amber when stale > 5 min)
   - body: CPU% + temp, RAM used/total bar, root-disk bar, uptime,
     VM rows (name + running/stopped dot), ollama model count
   - violet accent (#a78bfa) to distinguish from ARES red / ZEUS blue

2. Fleet / Backups panel — left column, after `#panel-ops`. id `panel-fleet`.
   - row `ARES -> ZEUS`  : fresh (< 36h green) / stale (amber) / FAILED (red)
   - row `ZEUS -> ARES`  : newest snapshot date age, same coloring
   - ZEUS reachability dot
   - coloring thresholds ported verbatim from the MOTD `_bk_out` / `_bk_in`
     (< 36h fresh, < 8d stale, else failed)

Both panels are hidden (`display:none`) until the first `/api/fleet` response,
same as the ZEUS-only panels do today.

## Failure honesty

- Collector stale (timer dead) -> `age_sec` grows -> panels go amber with
  "data Ns old". Never shows a dead collector as green.
- EROS unreachable -> explicit offline state, not blank.
- Atomic write (.tmp + rename) so the dashboard never reads a torn file.

## Out of scope (YAGNI)

- Per-machine historical sparklines/graphs — add when EROS runs real workload
  worth trending.
- Controlling EROS/ZEUS from the dashboard (start/stop VMs) — read-only for now.
- Browser-side peer fetch — not needed; the file path covers both boxes.

## New / changed artifacts

- NEW  system/fleet-collect.sh                 (host collector)
- NEW  systemd unit + timer  ares-fleet.{service,timer}  (host, every 60s)
- NEW  route  GET /api/fleet  in app.py         (clone of /api/outreach read pattern)
- EDIT templates/home.html                      (2 panels + 1 JS poll block)
- Writes ai_data/fleet.json                     (runtime artifact, gitignored)
