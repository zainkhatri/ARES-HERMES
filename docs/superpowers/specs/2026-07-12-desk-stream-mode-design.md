# Desk/Stream Dual-Mode for VM 200 — Design

**Date:** 2026-07-12
**Context:** Post-mortem of the 2026-07-11 "everything is slow" incident.

## Post-mortem (locked)

Root cause of the incident: **Win11 25H2 silently enabled VBS/Memory Integrity**, booting
Microsoft's Hyper-V *inside* the KVM guest (nested virtualization). On AMD this taxes every
privileged operation through two hypervisors: whole-OS sluggishness, CS2 at 8–30 fps,
CPU and GPU both idle (everything waiting, nothing computing), guest kernel time 35–44%,
host cores half idle. Timing coincided with the Moonlight/Sunshine setup, which misdirected
blame for a full night.

Real secondary problems stacked on top (each true, none the answer):
1. GPU PCIe link stuck at **Gen2** (¼ bandwidth) + power-state wedge after vfio resets — fixed by cold `qm stop/start`.
2. Display drifted to **WinDisc 1024×768 software fallback** (VDD disabled + OLED not attached at boot).
3. **Wallpaper Engine** at ~31% CPU constantly, respawning from Run key.
4. VM emulator/IO threads pinned to host CPUs 10–15 = **SMT siblings of the gaming vCPU cores 2–7**.

Structural disease: desk mode (OLED only) and stream mode (virtual display primary) have
opposite display requirements, and mode state lived in five scattered places (logon task,
PnP flips, Sunshine config, registry, ad-hoc edits) with nothing enforcing consistency.

## Invariants (never violated)

1. The GPU always has ≥1 attached, active display. The zero-display state (→ WinDisc
   1024×768, games refuse to launch, apps render invisibly) must be structurally impossible.
2. Every display/mode change goes through one logged path (timestamp, trigger,
   before/after). No more archaeology.
3. VBS/Memory Integrity stays off. Re-enablement by a Windows update is detected and
   surfaced, never silent.

## Decisions

- **Mode switching:** automatic on stream connect/disconnect (no manual toggle to remember).
- **Resolution:** 2560×1440 everywhere. No pixel-perfect Mac-native re-moding (it broke the
  display state machine repeatedly in the past).
- **Streaming server:** evaluate **Apollo** (maintained Sunshine fork with built-in
  per-session virtual display) first; fall back to Sunshine + always-on VDD if it fails.
- **Primitive:** never PnP enable/disable the virtual display as a mode switch. Mode = which
  display is PRIMARY (idempotent; failure degrades to "wrong primary", never "no display").

## Phase 0 — Apollo spike (decides Phase 2's size)

1. Snapshot VM 200 (disk-only, crash-consistent is acceptable).
2. Install Apollo over Sunshine (same config shape, web UI, ports — dashboard's 47984
   readiness probe keeps working).
3. Re-pair Moonlight from the Mac (requires Zain on the Mac for the PIN).
4. Acceptance: stream starts with OLED asleep at 1440p on Apollo's own virtual display;
   desk returns clean when the stream ends (OLED primary, no phantom monitor); dashboard
   tile flips Ready as before; CS2 at the desk unaffected.
5. **Pass** → uninstall the MTT VDD driver entirely; MultiMonitorTool + trampoline +
   prep-cmd display wiring all become dead code. **Fail** → restore snapshot, build the
   Sunshine fallback (below).

## Phase 1 — Guardrails (ship regardless; all low-risk)

- **Hookscript** (`/var/lib/vz/snippets/gpu-swap.sh`): post-start pins non-vCPU threads to
  host CPUs 0,1,8,9 (currently 10–15 = SMT siblings of the vCPUs — pipeline theft).
  Post-start also checks the 3080's PCIe link (`lspci LnkSta`) and logs loudly if it trained
  below 16GT/s (the Gen2 wedge recurs after vfio resets).
- **VBS policy keys** in guest (`HKLM\SOFTWARE\Policies\Microsoft\Windows\DeviceGuard`):
  policy-level off is respected by feature updates, unlike the state keys flipped last night.
- **VBS canary**: logon task writes `C:\gamemode\state.json` (mode, displays, VBS status,
  ok flag) and appends transitions to `C:\gamemode\display-mode.log`. Dashboard later reads
  this (Phase 2) and shows Desk / Streaming / **BROKEN** instead of just "ready".
- **Startup diet**: Wallpaper Engine stays removed. Epic, Discord, GameBar widgets,
  TranslucentTB out of autostart. Steam stays.
- **Snapshot before Windows updates** as standard practice: `qm snapshot 200 pre-update-<date>`.

## Phase 2 — Mode logic

**If Apollo passed:** desk mode is simply "OLED primary", asserted once at logon.
Apollo owns the stream-side display per-session. Only deliverable: `display-status.ps1`
(state file writer) + dashboard reading it.

**If Apollo failed (Sunshine fallback):** MTT VDD stays always-enabled (device level).
One script `display-mode.ps1`:
- `stream` — VDD primary @1440p, OLED secondary.
- `desk` — OLED primary; **refuses and logs** if the OLED is not attached/active.
- `auto` — active Sunshine session ? stream : desk (conservative: desk direction only
  acts when no session AND OLED attached).
Triggers: Sunshine `global_prep_cmd` (single-line JSON — multiline crash-loops Sunshine)
→ existing interactive-session trampoline task → script. At-logon `auto` + 5-minute `auto`
backstop task (safe now: idempotent primary-flips, no PnP churn — this is the reconciler
done right, and it covers Sunshine crashing mid-stream where `undo` never fires).

## Rollout order & testing (per council Executor)

1. Phase 1 host guardrails → test with a real `qm stop/start 200`, verify pinning +
   link speed. Rollback = hookscript `.bak`.
2. Phase 1 guest items (policy keys, diet, canary task) → verify state.json appears.
3. Phase 0 snapshot + Apollo install → verify port 47984 + web UI up.
4. Moonlight re-pair + E2E stream test **from the Mac while at the desk** (real client,
   both screens observable). Rollback = restore snapshot.
5. Phase 2 per Apollo outcome. Every transition visible in `display-mode.log`.

## Explicitly rejected

- Continuous aggressive reconciler that fights the seated user (the old
  GameModeDisplayRest failure mode).
- PnP enable/disable as a mode primitive.
- Pixel-perfect Mac-native streaming (manual desk-side experiment someday, never auto).
- PCIe auto-remediation (auto cold-restart risks fighting the GPU-loan protocol; log + flag,
  human restarts).
- Logon script that silently re-disables VBS mid-session (fights Windows on its turf;
  policy keys + snapshot + canary instead).
