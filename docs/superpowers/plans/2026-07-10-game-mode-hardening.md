# Game Mode Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline — tasks are live ops with user checkpoints). Steps use checkbox (`- [ ]`) syntax.

**Goal:** Execute the hardening spec (`docs/superpowers/specs/2026-07-10-game-mode-hardening-design.md`): desk guard, invisible watchdog, deferred verifications, second client, cleanup.

**Architecture:** All guest-side scripts are pre-written in `scripts/gamemode/` (desk-guard.ps1, launcher.vbs, idle-watchdog.ps1). Execution = deploy via `qm guest exec` (base64 → `[IO.File]::WriteAllBytes`, the proven pattern), wire configs, verify. Dashboard untouched.

## Global Constraints

- **DO NOT START until Zain says GO** — he is mid-comp; every task below touches the VM or needs him.
- Never modify the gpu-swap hookscript. Keep `ensure_only_display` and the clone resting state.
- Deploy files with the base64/WriteAllBytes pattern; interactive-session actions via a temporary `Register-ScheduledTask` (LogonType Interactive) trampoline — `quser`/`qwinsta`/`schtasks /np` are unavailable (Win11 Home) and guest-exec is session 0.
- Guest paths: scripts in `C:\gamemode\`; Sunshine config `C:\Program Files\Sunshine\config\sunshine.conf`; restart via `Restart-Service SunshineService`.
- Known values: VM IP 192.168.20.215, Sunshine creds REDACTED/REDACTED (PIN API: `curl -sk -u REDACTED:REDACTED -X POST https://192.168.20.215:47990/api/pin -d '{"pin":"XXXX","name":"pc"}'`), VDD config `C:\VirtualDisplayDriver\vdd_settings.xml`, loan flag `/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/.gpu-on-loan`, dashboard curl-with-login pattern in the launch plan Task 7.
- Git commits: subject only, no co-author.

---

### Task 1: Deploy desk guard

- [ ] **Step 1:** Deploy `scripts/gamemode/desk-guard.ps1` → `C:\gamemode\desk-guard.ps1` (base64 pattern). Verify byte length matches.
- [ ] **Step 2:** Add to sunshine.conf: `global_prep_cmd = [{"do":"powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\\gamemode\\desk-guard.ps1","undo":"","elevated":"false"}]` via `Add-Content` (single line, JSON-in-conf format). `Restart-Service SunshineService`.
- [ ] **Step 3:** Test BLOCK: Zain at desk (or fresh input via trampoline `SendInput`), then attempt a Moonlight connect from the Mac → launch must fail within ~2 s.
- [ ] **Step 4:** Test PASS: no console input for >2 min (or `New-Item C:\gamemode\force-stream.flag`), connect → stream launches. Remove the flag after.
- [ ] **Step 5:** Commit any tweaks to `scripts/gamemode/desk-guard.ps1`.

### Task 2: Re-enable invisible watchdog

- [ ] **Step 1:** Deploy `scripts/gamemode/launcher.vbs` → `C:\gamemode\launcher.vbs`.
- [ ] **Step 2:** Re-register task: action `wscript.exe C:\gamemode\launcher.vbs`, same 5-min repetition, user zainn interactive; `Enable-ScheduledTask GameModeIdleWatchdog`.
- [ ] **Step 3:** `Start-ScheduledTask` manually while Zain is at the desk → he confirms **no window flash**; watchdog exits 0 (desk input = not idle).

### Task 3: Verification debt

- [ ] **Step 1 (restore):** Zain at desk watching OLED: Mac stream connect (use force-stream.flag if desk hot) → OLED goes dark (only_display), disconnect → clone restores, OLED back. 30 s.
- [ ] **Step 2 (idle chain):** Leave VM untouched ≥35 min (no streams, no desk input; guard flag removed) → watchdog shuts down → hookscript post-stop → verify: `qm status 200` stopped, `pct exec 101 -- nvidia-smi -L` lists 3080, dashboard tile "Stopped · GPU home".
- [ ] **Step 3 (stopped curl):** In CT: login-cookie curl `POST /api/vm/status` → `running:false, streaming_ready:false, gpu_home:true`.
- [ ] **Step 4 (red tile):** `touch` the loan flag on the host (VM stopped) → tile shows GPU MISSING within one 15 s poll → `rm` flag → recovers.

### Task 4: Second PC client

- [ ] **Step 1:** Zain installs Moonlight on the PC, sends the 4-digit PIN; submit via the PIN API (name "pc").
- [ ] **Step 2:** Ask the PC monitor's native res/refresh; if missing from `vdd_settings.xml`, add a `<resolution>` block (and restart the VDD device or note it applies next reboot).
- [ ] **Step 3:** PC streams Desktop; stats overlay sane. Append result to this plan.

### Task 5: Cleanup + records

- [ ] **Step 1:** In guest: `Unregister-ScheduledTask CursorTrace`; delete `C:\gamemode\{mouse-inject.csv,mi.zip,cursor-trace.ps1,cursor-trace.csv,instrument-test.txt}`.
- [ ] **Step 2:** Zain's call on Parsec: if GO, uninstall via `winget uninstall Parsec` (or its uninstaller) + `pnputil` remove the Parsec VDA; verify display adapters shrink to 3.
- [ ] **Step 3:** Remind Zain: Tailscale admin → `supernas` → disable key expiry.
- [ ] **Step 4:** Update `scripts/gamemode/vdd-notes.md` (guard + launcher wiring), update memory files (`vm200-input-stutter` if anything new; add hardening outcome), commit all.
