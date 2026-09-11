# Homelab Architecture — ARES / EROS / ZEUS

Date: 2026-09-06
Status: Approved (design). Sub-projects specced/executed individually.

## Target end-state

**ARES** (main workstation + NAS) — Ryzen 9 5900XT, RTX 3080, 32GB, 916G NVMe (85% full)
- Files: photos (282G), projects (273G), personal, work; photo gallery; journals; dashboard
- Full **930GB Windows gaming VM** (VM200) — stays here, primary rig
- Headless on WiFi (`wlp6s0` → janjee, 10.0.1.66)

**EROS** (services + portable second seat) — Ryzen 7 5700X, GTX 1070, 16GB (32G installed, only 2 sticks train — mismatched-kit half-fail; recover in BIOS later), 512G SSD (348G free in local-lvm) + 1TB HDD (currently Windows/NTFS)
- **Business automation** — migrated off ZEUS (FAI/FCSF/Ibtakar Docker + Postgres)
- **Local model** — Ollama on the 1070, serves ARES dashboard + FAI (cuts Claude API spend)
- **Lean portable Windows** (~120GB) on the 512G SSD (local-lvm), one-active-at-a-time, ARES⇄EROS handoff sync
- Headless on WiFi (`wlo1` → janjee, 10.0.1.69, persistent); Tailscale `100.90.30.81`

**ZEUS** (the horcrux) — Ryzen 3 4300U, 16GB, 3× USB SSDs (T5/T7/T9)
- Retired from business duty → **offline backup mule**: wakes nightly, pulls VERSIONED backups of ARES + EROS, unmounts, sleeps. Air-gapped against ransomware/mistakes.

## Windows VM decision (locked)
Full 930GB bidirectional nightly sync over WiFi is infeasible (bandwidth + corruption + EROS RAM/disk). Chosen: **lean ~120GB portable Windows** on EROS's SSD, ZFS-replicated with a one-active-at-a-time handoff. ARES keeps the full 930GB gaming VM separately.

## Roadmap (dependency order)
1. **Business automation → EROS** (clean, high-value; frees ZEUS). 6.3G code + 122MB DB + ~10G images.
2. **Ollama on EROS's 1070** (NVIDIA driver + Ollama; repoint dashboard/FAI).
3. **ZEUS → offline horcrux** (versioned pull-backup of ARES + EROS; scheduled wake/sleep).
4. **Lean portable Windows** on EROS SSD; ARES⇄EROS handoff sync (recover 32GB RAM first, BIOS).

## Cheap wins (parallel)
- ZEUS WiFi persistence (so it never blacks out the business again).
- EROS Tailscale ACL fix (reach `eros` by name from the tailnet).
- Confirm ARES RAM trained at 3200 (DOCP).
- ARES at 85% — the horcrux + eventual cold-data offload matters.

## Hardware: no purchases needed to start
- Windows VM fits on EROS's existing 512G SSD (348G free). 32GB RAM already installed (needs BIOS coaxing to train all 4). Business + Ollama run fine on 16GB.

## Non-negotiable constraints
- **FCSF and FAI never intertwine** — separate tenants/tokens/crons; only `ibt-db` is shared (filter by tenant_id). Any migration preserves this.
- **fai-bdr is HERMES/ZEUS-only, never GitHub** — offsite = rsync, not GitHub.
- The business is **live revenue + a paying client** — migration must not double-send (both boxes firing outbound) and must not lose data. Cutover is atomic + verified.
