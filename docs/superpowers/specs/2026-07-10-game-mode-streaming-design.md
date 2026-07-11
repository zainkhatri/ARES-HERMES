# Game Mode: one-tap 3080 streaming (Sunshine/Moonlight)

**Date:** 2026-07-10
**Goal:** Beat Parsec. One tap in the dashboard boots the gaming VM (GPU swap included), and Moonlight on the Mac or the other PC streams at the client's native resolution/refresh (up to 1440p@120) over LAN or Tailscale. When play ends, the GPU returns to the gallery automatically.

## Current state (already built — do not rebuild)

- Hookscript `/var/lib/vz/snippets/gpu-swap.sh` handles the full 3080 handoff on `qm start/stop 200`: flag file + clean `ares.service` restart into CPU mode, vfio bind, and the reverse on stop. Logs to `/var/log/gpu-swap.log`.
- Dashboard (`app.py:2558`, `/api/vm/<action>`) already starts/stops VM 200 detached (`nohup qm … &`) and `/api/vm/status` reports VM state; `home.html` has the button + polling.
- VM 200 (`win11-gaming`): guest agent on, vCPU pinned 2-7, latency-tuned, both USB controllers passed through, NIC on vmbr0, physical monitor on the 3080 for desk play.

## Gaps this project fills

1. No streaming server in the VM.
2. Dashboard knows "VM running" but not "Sunshine accepting connections."
3. Streaming resolution would be stuck at the physical monitor's mode.
4. Nothing returns the GPU if you walk away without shutting down.

## Design

### Inside the VM (Windows)

- **Sunshine** installed as a Windows service (auto-start at boot). Encoder: NVENC HEVC (the 3080 has no AV1 encode). Bitrate ceiling 150 Mbps.
- **Virtual Display Driver** (MikeTheTech MTT VDD). Sunshine `prep-cmd` on stream start switches output to the virtual display at the connecting client's exact resolution/refresh; on stream end, restores the physical monitor. Desk play is untouched — VDD is only active during a stream.
- **Tailscale** installed in the guest so Moonlight works off-LAN. On LAN, clients discover the VM directly over vmbr0.
- **Idle watchdog**: PowerShell scheduled task (runs every 5 min): if no active Sunshine session AND user input idle > 30 min → `shutdown /s /t 60` with an on-screen cancel warning. Clean guest shutdown fires the hookscript post-stop and the GPU returns to LXC 101. Desk play generates input, so the watchdog never fires mid-session.

### Host / dashboard (small diffs only)

- `/api/vm/status` adds one field: `streaming_ready` — a TCP connect probe from the host to the VM's IP on Sunshine's HTTPS port (47984), timeout 1 s. VM IP resolved via guest agent (`qm agent 200 network-get-interfaces`), cached while the VM runs.
- `home.html` VM tile becomes a 3-state indicator driven by existing polling: **swapping GPU → booting Windows → ready to stream**. No new endpoints, no new host services.

### Clients

- Moonlight on the Mac and the other PC. One-time pairing via Sunshine web UI (`https://<vm>:47990`, PIN).
- Per-client profiles: Mac at panel-native res/refresh (120fps if ProMotion), PC at its monitor's native mode. LAN target 1440p@120 @ 80–150 Mbps; remote over Tailscale target 1080p@60 (upload-bound).

## Flow

1. Tap Game Mode on dashboard (any device, auth-gated).
2. Existing detached `qm start 200` → hookscript swaps GPU → Windows boots → Sunshine service up.
3. Tile flips to "ready to stream" (~60–90 s total). Open Moonlight, connect.
4. Stream start: VDD activates at client mode. Stream end: physical monitor restored.
5. Done playing: tap stop in dashboard, or shut down from Windows, or let the 30-min idle watchdog do it. Hookscript hands the 3080 back to CLIP.

## Failure handling

Nothing new invented. Hookscript already aborts cleanly on unbind timeout and logs everything. If Sunshine never comes up, the tile stays at "booting Windows" and the VM console (existing noVNC path) is the debug route. The status probe failing is indistinguishable from "not ready" — safe default. Idle watchdog uses `shutdown /s` (never force) so games/saves get normal Windows shutdown semantics.

## Testing

- Tap-to-play end-to-end from Mac and PC, on LAN and via Tailscale.
- Desk-play regression: physical monitor + passed-through USB unaffected after a stream session.
- VDD switch: stream from Mac (client-native mode active), end stream (physical monitor restored).
- Idle watchdog: leave VM idle with no session → shuts down at ~30 min; verify GPU back in LXC 101 (CLIP search works, `nvidia-smi` in CT).
- Full cycle twice in a row (wedge regression — GSP firmware already disabled via `NVreg_EnableGpuFirmware=0`).

## Out of scope (add later only if wanted)

- Moonlight "Wake PC" / WoL listener on the host (dashboard button is the one tap).
- AV1 encode (no 3080 support), HDR tuning, Apple TV/mobile clients.
- Any changes to the hookscript or swap protocol — it's the contract, we build on top.
