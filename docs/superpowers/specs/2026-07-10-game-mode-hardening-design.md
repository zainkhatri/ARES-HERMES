# Game Mode hardening

**Date:** 2026-07-10 (evening, post-launch)
**Goal:** Make Game Mode boring and reliable after launch-day incidents: a stream must never blind the desk player, the watchdog must be invisible, and every deferred verification gets paid.

## Incidents driving this

1. Stream/display operations while Zain sat at the OLED (Odyssey G60SD, 2560x1440@360) deactivated his display mid-game (`ensure_only_display` doing its job at the wrong time).
2. The idle watchdog's scheduled task flashed a console window every 5 min during comp — task disabled as emergency fix.
3. Several plan verifications were deferred during the launch chaos.

## Design

### 1. Desk guard — block streams while the desk is hot

`C:\gamemode\desk-guard.ps1`, wired as a Sunshine **global prep command** (runs synchronously in the user session before every stream; non-zero exit aborts the launch):

- Console input within the last **120 s** → `exit 1` (Moonlight shows launch failure; desk player never interrupted).
- Else, or if `C:\gamemode\force-stream.flag` exists and is younger than 10 min → `exit 0`.
- The flag is a manual override for "walking away right now" (create via any shell). A dashboard button can write it later if the 2-min wait proves annoying — not built now.

Why prep-cmd: Moonlight connects directly to Sunshine; the dashboard is not in the connection path. Prep-cmd is the only synchronous chokepoint that can veto a launch.

### 2. Invisible watchdog

`C:\gamemode\launcher.vbs` (wscript, no console window) runs `powershell -File idle-watchdog.ps1` hidden. `GameModeIdleWatchdog` task re-pointed at `wscript.exe launcher.vbs`, re-enabled, still every 5 min in the interactive session. Watchdog logic unchanged (NVENC session / input idle / net+GPU busy checks).

### 3. Verification debt

- Stream connect→disconnect with Zain watching the OLED: clone topology restores on stream end.
- True-idle 35 min: watchdog fires `shutdown /s /t 300`, hookscript returns GPU, tile shows "Stopped · GPU home", `nvidia-smi -L` works in CT 101.
- Stopped-state `/api/vm/status`: `running:false, streaming_ready:false, gpu_home:true`.
- GPU MISSING tile: `touch`/`rm` the loan flag while stopped → red tile appears/clears.

### 4. Second client

Pair the other PC via Sunshine PIN API; add its monitor's native mode to `vdd_settings.xml`.

### 5. Cleanup

- Delete `CursorTrace` task and `C:\gamemode\{mouse-inject.csv,mi.zip,cursor-trace.*,instrument-test.txt}`.
- Uninstall Parsec app + its Virtual Display Adapter (replaced by Sunshine/VDD; needs Zain's confirmation).
- Zain: disable Tailscale key expiry for `supernas` in the admin console.
- Update `scripts/gamemode/` records and memory files at the end.

## Unchanged (guardrails)

- gpu-swap hookscript and flag protocol: untouched.
- `ensure_only_display` + clone resting state: kept (streaming stays single-display to avoid cursor-escape; desk guard handles the collision case).
- Dashboard endpoints/tile: as shipped.
- Rule from tonight, now standing policy: no display-topology or interactive-session operations without checking whether someone is at the console (`desk-guard.ps1` is also the reusable check).

## Execution constraint

Nothing runs against the VM until Zain says GO (he is mid-comp). All prep is repo-side only.
