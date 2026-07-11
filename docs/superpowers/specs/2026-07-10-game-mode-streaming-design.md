# Game Mode: one-tap 3080 streaming (Sunshine/Moonlight)

**Date:** 2026-07-10 (amended same day after council review)
**Goal:** Beat Parsec. One tap in the dashboard boots the gaming VM (GPU swap included), and Moonlight on the Mac or the other PC streams at the client's native resolution/refresh (up to 1440p@120) over LAN or Tailscale. When play ends, the GPU returns to the gallery automatically — with verification, because a wedged 3080 is only recoverable by physically powering off the host.

## Current state (already built — do not rebuild)

- Hookscript `/var/lib/vz/snippets/gpu-swap.sh` handles the full 3080 handoff on `qm start/stop 200`: flag file + clean `ares.service` restart into CPU mode (gallery degrades gracefully to CPU-only CLIP), vfio bind, and the reverse on stop. Logs to `/var/log/gpu-swap.log`.
- Dashboard (`app.py:2558`, `/api/vm/<action>`) already starts/stops VM 200 detached (`nohup qm … &`) and `/api/vm/status` reports VM state; `home.html` has the button + polling.
- VM 200 (`win11-gaming`): guest agent on, vCPU pinned 2-7, latency-tuned, both USB controllers passed through, physical monitor on the 3080, **and `vga: std`** — so the VM has three display adapters (physical, emulated, later VDD) and noVNC console works as a fallback.

## Gate test (before any implementation)

Boot VM 200, shut it down **from inside Windows** (guest-initiated, not `qm stop`), then verify `/var/log/gpu-swap.log` post-stop ran and `nvidia-smi` works in LXC 101. The hookscript is battle-tested via `qm start/stop`; guest-initiated shutdown routes through qmeventd → `qm cleanup` — a path it has never run here, and the path the idle watchdog depends on. If this fails, Phase 4 is cancelled and shutdown stays dashboard-driven.

## Design — phased, streaming first

### Phase 1: Sunshine + clients (this alone beats Parsec)

- Sunshine as a Windows service (auto-start at boot), NVENC HEVC (3080 has no AV1 encode), bitrate ceiling 150 Mbps. Windows firewall rules for Sunshine ports. Setup via RDP (guest-agent exec and noVNC are poor for interactive first-run).
- Sunshine `output_name` pinned to the physical monitor — with three adapters present, never let it auto-pick.
- Tailscale in the guest (pre-generated auth key; note: node keys expire ~180 days unless marked non-expiring — mark it non-expiring).
- Moonlight paired on Mac + PC via Sunshine web UI PIN. Stream on the physical monitor's mode. LAN target 1440p@120 @ 80–150 Mbps; Tailscale remote target 1080p@60 (upload-bound).
- Give the VM a static IP / DHCP reservation (Phase 3 depends on it).

### Phase 2: Virtual Display Driver

- MikeTheTech MTT VDD + Sunshine prep-cmd (windowsdisplaymanager script stack): switch to virtual display at the connecting client's res/refresh on stream start, restore physical monitor on end.
- **Dirty-disconnect handling is part of this phase, not an afterthought:** a crashed Moonlight/WiFi drop can skip the restore and leave the desk monitor black. Mitigations: Sunshine's session-end (not just graceful-quit) hook runs the restore; a startup task also restores the physical display on boot, so a reboot always recovers the desk. noVNC (`vga: std`) remains the blind-debug route.
- Test switch/restore repeatedly, including monitor-asleep and mid-stream kill.

### Phase 3: Dashboard readiness + reclaim verification

- `/api/vm/status` adds `streaming_ready`: plain TCP connect to the VM's **static IP** on Sunshine's HTTPS port (47984), timeout 1 s, from the dashboard process itself (LXC 101 shares vmbr0 — no `qm agent` call, which LXC 101 cannot run).
- Tile becomes a 4-state indicator: **off / GPU home → swapping GPU → booting Windows → ready to stream**.
- **Post-reclaim health check:** after VM stop, the dashboard verifies the GPU actually came home (CLIP/NVENC init OK — effectively `nvidia-smi` succeeding inside the CT via the existing app startup path) and shows an explicit error state if not. No green tile over a dead GPU.

### Phase 4: Idle watchdog (only if the gate test passed)

- PowerShell scheduled task, every 5 min, running **in the interactive session** (requires Windows autologon — GetLastInputInfo reads garbage from SYSTEM).
- Shutdown condition — all three, not just input idle: no active Sunshine session (no established connection on 47998–48010 via `Get-NetTCPConnection`) AND input idle > 30 min AND no foreground work (network throughput and GPU utilization below thresholds — a 100 GB Steam download must never be killed).
- `shutdown /s /t 300` (never force). If shutdown is blocked (Windows Update, unsaved-work dialog) and the VM is still up 15 min later, the dashboard's existing status polling shows it — the fallback is the existing manual stop button, not an escalation ladder.
- Deliberate trade-off, written down: every automated swap is wedge exposure (GSP history; live wedge = physical power-off, taking gallery/dashboard/AdGuard down). The gate test, GSP-disabled driver config, and post-reclaim health check are the mitigations. If wedges recur, the watchdog is the first thing to disable.

## Flow

1. Tap Game Mode on dashboard (any device, auth-gated).
2. Existing detached `qm start 200` → hookscript swaps GPU → Windows boots → Sunshine service up.
3. Tile flips to "ready to stream" (~60–90 s). Open Moonlight, connect. Stream start: VDD at client mode. Stream end (clean or dirty): physical monitor restored.
4. Done playing: dashboard stop, Windows shutdown, or the watchdog. Hookscript returns the 3080; dashboard verifies the reclaim and shows "GPU home" (or an error state).

## Testing

- Gate test (above) — first, before any code.
- Tap-to-play E2E from Mac and PC, LAN + Tailscale.
- Desk-play regression: physical monitor + passed-through USB unaffected after a stream session.
- VDD: clean end restores monitor; **killed stream** restores monitor (session-end hook); reboot restores monitor.
- Watchdog negative cases: active Steam download survives; desk session survives; stream session survives. Positive case: true idle → shutdown → GPU verified back in LXC 101.
- Full swap cycle twice in a row (wedge regression; `NVreg_EnableGpuFirmware=0` already set).

## Later (explicitly deferred, near-zero cost when wanted)

- iOS app "Play" button → boot endpoint → push on `streaming_ready` → `moonlight://` deep link (iPad + controller = handheld).
- Generalized "GPU Mode: Gallery / Gaming / LLM" tile (hookscript already supports VM 300).
- Sunshine desktop app entry = remote Windows workstation on the Mac (free side effect; no work needed).
- WoL listener, AV1/HDR, Apple TV clients.
- Any changes to the hookscript/swap protocol — it's the contract, we build on top.
