# ZEUS Horcrux — nightly wake→backup→sleep backup mule

Date: 2026-09-07
Status: Approved (design). Implementation follows in verified stages.

## Purpose
Turn ZEUS (retired from business duty after the 2026-09-07 business cutover to EROS) into an
air-gapped-most-of-the-day backup mule: it powers ON at 04:00, pulls versioned backups of ARES
and EROS, publishes a completion stamp, then powers OFF. Its data disks are mounted only during
the backup window, so they are inert (unpowered/unmounted) ~23.5 h/day — minimizing corruption
and ransomware exposure.

## Fixed facts (verified 2026-09-07)
- **Topology:** all three boxes co-located on the `janjee` LAN — ARES `10.0.1.66`, EROS `10.0.1.69`,
  ZEUS `10.0.1.67` (enp1s0) / `10.0.1.70` (wlp2s0). Tailnet also exists but EROS↔peers ACL is broken;
  **use LAN IPs**.
- **ZEUS wake:** `rtcwake` installed, `/sys/class/rtc/rtc0/wakealarm` present, RTC alarm supported.
  S5-wake is BIOS-dependent (`Default string` board) → MUST be validated before relied upon.
- **WoL:** ZEUS `enp1s0` = `Supports Wake-on: pumbg`, currently `Wake-on: g` (magic packet armed).
  ARES/EROS are same-LAN → can send the WoL packet. Second, independent wake path.
- **ZEUS disks:** OS on `sdb` (stays mounted). Data disks `sda` (`de676cab…`, has PROMETHEUS_BACKUP)
  and `sdc` (`27b8f17b…`, old business disk) are the mount-only-when-needed backup targets.
- **ZEUS→EROS:** LAN ping OK to `10.0.1.69`; needs ZEUS's SSH key added to EROS `authorized_keys`.
- **ZEUS→ARES:** already trusted (old backup mesh worked).

## Lifecycle (one cycle/day)
1. **04:00 wake** via RTC alarm (armed before the prior power-off). WoL from ARES/EROS = fallback.
2. **Boot →** `zeus-horcrux.service` (systemd oneshot, `WantedBy=multi-user.target`) runs the orchestrator.
3. **Mount** the backup data disk on demand (removed from fstab auto-mount; orchestrator mounts explicitly).
4. **Pull, versioned** (`rsync -a --delete --link-dest=<prev snapshot>` into a dated dir):
   - **ARES** (`root@10.0.1.66`): `PHOTOS PROJECTS PERSONAL WORK RESUME` under `/mnt/nvme/PROMETHEUS/`.
   - **EROS** (`root@10.0.1.69`): `BUSINESS/AUTOMATION-IBT` code + all `.env`/configs, EXCLUDING
     `node_modules` and Postgres raw data dir; PLUS a fresh consistent dump:
     `ssh eros 'docker exec ibt-db pg_dump -U ibt -d ibt --clean --if-exists --no-owner' > ibt-<ts>.sql`.
5. **Verify** rsync return codes (0/24 ok; 24 = vanished-source-file, benign) + `pg_dump` size sanity
   (> 20 MB). Write a completion stamp `BACKUP_OK <epoch>` locally.
6. **Prune** to the last **14** dated snapshots (hardlinks make older ones cheap).
7. **Publish** the completion stamp to ARES and EROS (`/…/INFRA/status/zeus-backup-stamp`) for the
   staleness watchdog.
8. **Unmount** cleanly (`sync && umount`), **arm next 04:00** (`rtcwake -m off -t <next 4am epoch>`),
   which powers off.

## Anti-lockout (critical safety)
- **Validation-first rollout:** nightly power-off is NOT enabled until a live `rtcwake -m off` test with
  a ~3-min alarm proves ZEUS wakes (WoL staged as backup during the test). If S5-wake fails → fall back
  to WoL-triggered wake or S3 suspend before committing.
- **Morning staleness watchdog on ARES** (~06:00, always-on box): if ZEUS's backup stamp is missing/stale
  → alert + auto-WoL-retry once.
- **Fail-safe branches in the orchestrator:**
  - mount failure → **stay ON + alert** (do not power off; disk needs inspection).
  - any pull failure → complete what it can, publish PARTIAL status, still proceed to arm+off
    (the watchdog surfaces partial/stale).
  - `rtcwake` arm failure → **stay ON + alert** (never power off without a wake armed).
- **Maintenance override:** presence of `/root/zeus-stay-awake` skips the auto-poweroff for hands-on work.

## Discontinue ZEUS (transition — done first, before the horcrux loop)
- Stop business remnants: `ibt-db`, `cc-proxy`, `ibtakar-snap-proxy` (business now authoritative on EROS).
- Remove ZEUS's 4 leftover backup-mesh crons (`nexus-backup-to-ares`, `ibt-hourly-dump`, `sidekick-sync`,
  `ares-watch`) — replaced by this pull model.
- Retire the obsolete ARES-side HERMES-SIDEKICK failover (ZEUS-primary is gone): disable ARES crons
  `sidekick-checker.sh`, `primary-ok-refresh.sh` (keep code/snapshots for reference). MODE was already
  `observe`/unarmed.

## Components (small, single-purpose)
- `zeus-horcrux.sh` (on ZEUS) — the orchestrator (mount → pull ARES → pull EROS+dump → verify → prune →
  publish → unmount → arm-wake → poweroff), with the fail-safe branches above.
- `zeus-horcrux.service` (on ZEUS) — systemd oneshot, runs the orchestrator on boot.
- `zeus-wake-check.sh` (on ARES) — 06:00 cron: read ZEUS's stamp, alert if stale, WoL-retry once.
- WoL persistence (on ZEUS) — keep `enp1s0` Wake-on: g across reboots (systemd/`ethtool` unit); record MAC.

## Rollout order (validation-first)
1. Discontinue ZEUS business + retire ARES sidekick.
2. ZEUS→EROS SSH key; WoL persistence + record MAC; ARES/EROS WoL sender check.
3. Write `zeus-horcrux.sh`; **dry-run** the backup (mount + pull + dump + verify + prune + unmount)
   WITHOUT the poweroff — confirm a full versioned snapshot lands.
4. Install `zeus-horcrux.service` (boot-run, but with poweroff still gated off).
5. **RTC-wake test** (`rtcwake -m off -t now+3min`, WoL ready) — confirm ZEUS wakes.
6. Only after 5 passes: enable the arm-wake+poweroff tail + the ARES staleness watchdog.

## Open risks
- S5-wake unproven until step 5; WoL is the mitigation, validated same-LAN.
- ZEUS on WiFi (`wlp2s0`) as well as ethernet — WoL needs the **wired** `enp1s0` powered; confirm ZEUS
  is cabled (it reports a wired IP `10.0.1.67`, so yes).
- Backup disk capacity: ARES PHOTOS+PROJECTS ≈ 555 G vs ZEUS 2×931 G data disks — fits with 14 hardlinked
  snapshots; monitor after first run.
