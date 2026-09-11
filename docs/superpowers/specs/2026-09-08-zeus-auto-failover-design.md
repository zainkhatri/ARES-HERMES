# ZEUS Auto-Failover — automatic takeover when ARES or EROS goes down

Date: 2026-09-08
Status: Approved (fenced-auto). Build + test follows.

## Goal
Automatic (no human) failover: the **surviving box** detects its peer is down → **WoL-wakes ZEUS** →
ZEUS takes over. Preserves the air-gap (ZEUS still sleeps 23.5h/day; the watcher lives on the always-on
surviving box, not on ZEUS). Business side is **fenced** so a false "EROS down" can never double-send to
the paying client (FCSF).

## Topology
```
EROS ──watches──▶ ARES     ARES down  → WoL ZEUS → zeus-restore-ares.sh --serve   (SAFE, read-only)
ARES ──watches──▶ EROS     EROS down  → FENCE EROS → WoL ZEUS → zeus-restore-eros.sh --failover-auto (FENCED)
```
A WoL failover-boot lands outside 03–06h, so the horcrux maintenance-guard keeps ZEUS idle (no
backup/poweroff) and it stays up to serve.

## Safety model (the crux — business side)
**The danger:** ARES falsely thinks EROS is down (network blip) → ZEUS sends while EROS is *also* sending
→ duplicate emails to FCSF's prospects.

**Four rules that make a false positive impossible-to-double-send:**
1. **Conservative multi-signal detection.** EROS is "down" only after **≥5 consecutive minute-checks fail**,
   across MULTIPLE signals: LAN ping (10.0.1.69) + tailnet + business health (a daemon port) + DB reachability.
   Any one signal recovering resets the counter.
2. **Self-connectivity gate.** Before failing over, ARES verifies IT is healthy (can reach its gateway +
   ZEUS). If ARES is the isolated one → **ABORT** (it's ARES's problem, not EROS's — classic split-brain guard).
3. **Fence before send.**
   - EROS reachable at all → ARES SSHes in and forcibly stops senders (`docker compose stop`) + stops the
     permit timer + clears business crons. Definitive: EROS *cannot* send.
   - EROS fully unreachable → it also can't reach Gmail (no network path) → cannot send → safe to take over.
   - Independent second gate: ZEUS, before its first send, confirms EROS's published heartbeat/permit is
     STALE (>10 min). If it's fresh, ZEUS refuses (EROS is alive somewhere).
4. **No auto-failback (hysteresis).** Once failed over, never auto-revert (prevents ping-pong flapping).
   Alert the user; failback is a deliberate command.

## ARES side (photo/dashboard) — lower stakes
Read-only serve, no double-anything possible. Simpler: ≥5 failed checks + EROS-self-healthy → WoL ZEUS →
`zeus-restore-ares.sh --serve`. A false positive just spins up a duplicate read-only gallery (harmless).

## Components
- **ZEUS:** add `zeus-restore-eros.sh --failover-auto` (no interactive prompt; only the fenced watcher calls it;
  includes the "EROS heartbeat stale?" gate). `zeus-restore-ares.sh --serve` already non-interactive.
- **EROS:** `/usr/local/sbin/watch-ares.sh` + systemd timer (60s). ARES-down → WoL + SSH ZEUS → ares-serve.
- **ARES:** `/usr/local/sbin/watch-eros.sh` + systemd timer (60s). EROS-down → self-check → fence → WoL +
  SSH ZEUS → eros-failover-auto.
- Both: **state file** (fail-counter + failed-over flag) for hysteresis; **notify** the user on failover.

## Notifications
Failover is a major event — the user MUST know. Write a prominent alert file + fire the existing alert
channel (Slack webhook / dashboard banner). On peer-recovery: alert "peer is back — run failback".

## Testing (mandatory before trusting — it can send to a paying client)
1. **Dry-run mode** (`WATCH_DRYRUN=1`): watchers run the full detect→self-check→fence *decision* and LOG it,
   executing nothing. Confirm logic on live-healthy peers (should never trip).
2. **Simulated EROS-down**: block ARES→EROS reachability; confirm detection fires only after 5 min, self-check
   passes, fence is attempted, ZEUS wakes, and failover runs against an **egress-blocked ZEUS** (no real send).
3. **False-positive**: a 1-2 min blip → confirm NO failover (counter/hysteresis holds).
4. **ARES-isolated**: ARES loses all connectivity → confirm ABORT (no failover).
5. **ARES-down**: confirm EROS detects + wakes ZEUS + gallery serves.

## Failback
Manual + alerted. When the dead box returns: stop ZEUS's takeover, re-sync state back to the recovered box,
bring it live, clear the failed-over flag. Documented in `ZEUS-FAILOVER-RUNBOOK.md`.

## Relation to the retired sidekick
This is a fresh, simpler, WoL-triggered system — NOT a revival of HERMES-SIDEKICK (which was the inverse:
ARES-as-failover-for-ZEUS-business, always-on, permit/fence-heavy). Reuse the *concepts* (fence, heartbeat),
not the code.
