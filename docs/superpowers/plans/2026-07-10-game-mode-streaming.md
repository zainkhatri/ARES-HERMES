# Game Mode Streaming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One-tap Sunshine/Moonlight game streaming from VM 200 (win11-gaming) with automatic GPU return to LXC 101, per the spec at `docs/superpowers/specs/2026-07-10-game-mode-streaming-design.md`.

**Architecture:** Four phases building on the existing GPU-swap hookscript (untouchable contract). Phase 1: Sunshine + Tailscale in the Windows guest, Moonlight clients paired. Phase 2: Virtual Display Driver with crash-safe restore. Phase 3: dashboard readiness probe + 4-state tile + reclaim verification. Phase 4: idle watchdog (gated on Task 1 passing).

**Tech Stack:** Proxmox `qm guest exec` (PowerShell in guest), Sunshine, MTT Virtual Display Driver, MultiMonitorTool, Tailscale, Flask (app.py), vanilla JS (home.html).

## Global Constraints

- **Never modify** `/var/lib/vz/snippets/gpu-swap.sh` or the flag+restart protocol. Never kill GPU PIDs.
- Any `qm start/stop 200` from the dashboard must stay **detached** (`nohup … &` over ssh) — the hookscript restarts ares.service mid-handoff.
- Dashboard code lives on the host at `/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/`; after editing, restart with `pct exec 101 -- systemctl restart ares`.
- This repo has no pytest suite; verification for dashboard changes = `curl` against the live service (auth: reuse an authed session cookie or temporarily test with `curl -k https://…` through the login — see Task 7 Step 4 note).
- Git commits: subject line only, no body, no co-author trailers.
- Executor context: you are root on the ARES Proxmox host. Guest commands run via `qm guest exec 200 --timeout <s> -- <cmd>` (returns JSON; check `exitcode` in `out-data`). Interactive Windows steps (pairing PIN, autologon password) are **user steps** — print instructions and wait.
- Known addresses: host `192.168.20.51`, VM 200 `192.168.20.215` (existing DHCP reservation — verify in Task 1), LXC 101 is CT `101`.
- Sunshine ports: 47984 (HTTPS/probe), 47989 (HTTP), 47990 (web UI), 48010 + 47998-48000 (session).

---

### Task 1: Gate test — guest-initiated shutdown must run the hookscript

The entire auto-return story (and Phase 4's existence) depends on guest-initiated shutdown firing the hookscript post-stop via qmeventd. Prove it before building anything.

**Files:** none (verification only).

**Interfaces:**
- Produces: GO/NO-GO decision for Task 9 (idle watchdog). Record the result at the bottom of this plan file.

- [ ] **Step 1: Record pre-state**

Run: `tail -5 /var/log/gpu-swap.log && ls /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/.gpu-on-loan 2>&1 && pct exec 101 -- nvidia-smi -L`
Expected: no flag file (`No such file`), `GPU 0: NVIDIA GeForce RTX 3080` from the CT.

- [ ] **Step 2: Start the VM (detached, hookscript does the swap)**

Run: `nohup qm start 200 >>/var/log/qm-start-200.log 2>&1 & sleep 90; qm status 200`
Expected: `status: running`. Then `tail -20 /var/log/gpu-swap.log` shows pre-start ran and the flag was created.

- [ ] **Step 3: Shut down FROM INSIDE the guest**

Run: `qm guest exec 200 --timeout 30 -- shutdown /s /t 5`
Expected: JSON with `exitcode: 0`. This is a guest-initiated shutdown — Windows tells QEMU to stop, which routes through qmeventd → `qm cleanup`, not `qm stop`.

- [ ] **Step 4: Verify the hookscript post-stop ran**

Run: `sleep 120; qm status 200; tail -20 /var/log/gpu-swap.log; ls /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/.gpu-on-loan 2>&1; pct exec 101 -- nvidia-smi -L`
Expected: `status: stopped`; log shows post-stop entries with a timestamp AFTER Step 3; flag file gone; `nvidia-smi -L` in the CT lists the 3080.

- [ ] **Step 5: Record the verdict**

Append to this plan file under "Gate test result": PASS (post-stop ran on guest shutdown) or FAIL (log shows no post-stop → Task 9 is cancelled; all shutdowns stay dashboard-driven). Commit: `git add docs/superpowers/plans/2026-07-10-game-mode-streaming.md && git commit -m "Record game-mode gate test result"`

---

### Task 2: Install Sunshine in the guest, pinned to the physical monitor

**Files:**
- Create: `scripts/gamemode/install-sunshine.ps1` (in this repo, for the record; executed in-guest)

**Interfaces:**
- Produces: Sunshine service answering TCP 47984 on 192.168.20.215; web UI credentials known to the user; `output_name` pinned. Tasks 4, 6, 7 depend on port 47984 being up whenever the VM runs.

- [ ] **Step 1: Boot the VM and confirm the IP**

Run: `nohup qm start 200 >>/var/log/qm-start-200.log 2>&1 & sleep 90; qm guest exec 200 --timeout 30 -- powershell -NoProfile -Command "(Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias Ethernet*).IPAddress"`
Expected: `192.168.20.215` in out-data. If different, update the constant everywhere in later tasks (and prefer fixing the DHCP reservation instead).

- [ ] **Step 2: Download and silently install Sunshine in the guest**

Run (host):
```bash
qm guest exec 200 --timeout 600 -- powershell -NoProfile -Command "\
  \$u = (Invoke-RestMethod https://api.github.com/repos/LizardByte/Sunshine/releases/latest).assets \
    | Where-Object name -like '*windows-amd64-installer.exe' | Select-Object -First 1 -ExpandProperty browser_download_url; \
  Invoke-WebRequest \$u -OutFile C:\\Windows\\Temp\\sunshine.exe; \
  Start-Process C:\\Windows\\Temp\\sunshine.exe -ArgumentList '/S' -Wait; \
  Get-Service SunshineService | Select-Object Status,StartType"
```
Expected: `exitcode: 0`, service `Running` / `Automatic`. Save this command block into `scripts/gamemode/install-sunshine.ps1` verbatim (guest-side portion).

- [ ] **Step 3: Set web UI credentials**

USER STEP — print: "Pick Sunshine web credentials. I'll set them with: `qm guest exec 200 -- cmd /c \"\\\"C:\\Program Files\\Sunshine\\sunshine.exe\\\" --creds USER PASS\"` — tell me the username/password to use (or run it yourself in the VM)." Wait for the user, then run it and restart the service: `qm guest exec 200 --timeout 60 -- powershell -NoProfile -Command "Restart-Service SunshineService"`

- [ ] **Step 4: Pin output_name to the physical monitor**

The VM has three display adapters (3080-physical, `vga: std` emulated, later VDD). List outputs from the Sunshine log, then pin:
```bash
qm guest exec 200 --timeout 60 -- powershell -NoProfile -Command "\
  Select-String -Path 'C:\\Program Files\\Sunshine\\config\\sunshine.log' -Pattern 'Output Name' | Select-Object -Last 10"
```
Identify the output whose description matches the physical monitor (NVIDIA adapter, non-VDD). Then append `output_name = <that value>` (e.g. `\\.\DISPLAY1`) to `C:\Program Files\Sunshine\config\sunshine.conf` via guest exec (`Add-Content`), and restart SunshineService.
Expected: after restart, log shows the pinned output selected.

- [ ] **Step 5: Verify the probe port from the host**

Run: `timeout 3 bash -c 'cat < /dev/null > /dev/tcp/192.168.20.215/47984' && echo OPEN`
Expected: `OPEN`. (Sunshine's installer adds its own Windows firewall rules; if this fails, add them: `New-NetFirewallRule -DisplayName Sunshine -Direction Inbound -Action Allow -Protocol TCP -LocalPort 47984,47989,47990,48010` plus the same for UDP 47998-48000.)

- [ ] **Step 6: Commit the script record**

`git add scripts/gamemode/install-sunshine.ps1 && git commit -m "Add Sunshine install script for VM 200"`

---

### Task 3: Tailscale in the guest

**Files:** none in repo.

**Interfaces:**
- Produces: VM 200 reachable at a stable Tailscale IP from off-LAN. Task 4's remote test uses it.

- [ ] **Step 1: Get an auth key (user step)**

USER STEP — print: "Generate a Tailscale auth key (admin console → Settings → Keys → reusable off, pre-authorized on) and paste it here. After it joins, set the node's key expiry to disabled in the admin console — otherwise remote play dies in ~180 days."

- [ ] **Step 2: Silent install + join**

```bash
qm guest exec 200 --timeout 600 -- powershell -NoProfile -Command "\
  Invoke-WebRequest https://pkgs.tailscale.com/stable/tailscale-setup-latest-amd64.msi -OutFile C:\\Windows\\Temp\\ts.msi; \
  Start-Process msiexec -ArgumentList '/i C:\\Windows\\Temp\\ts.msi /qn' -Wait; \
  & 'C:\\Program Files\\Tailscale\\tailscale.exe' up --authkey=<PASTED_KEY> --unattended; \
  & 'C:\\Program Files\\Tailscale\\tailscale.exe' ip -4"
```
Expected: `exitcode: 0` and a `100.x.y.z` address. `--unattended` keeps Tailscale up when no user is logged in. Record the IP in this plan file. → **Joined 2026-07-10: 100.105.159.98** (hostname `supernas`). User reminded to disable key expiry.

- [ ] **Step 3: Verify from the host's tailnet**

Run: `tailscale ping <vm-tailscale-ip> | head -3`
Expected: pong replies. Remind the user to disable key expiry now.

---

### Task 4: Pair Moonlight on Mac + PC, first real streams (Phase 1 complete)

**Files:** none.

**Interfaces:**
- Produces: two paired clients; confirmed streaming on the physical monitor at its native mode, LAN and Tailscale. This is the "beats Parsec" milestone.

- [ ] **Step 1: Pairing ceremony (user steps)**

Print for the user:
1. Install Moonlight on the Mac (`brew install --cask moonlight`) and on the other PC (moonlight-stream.org).
2. In Moonlight, the host `192.168.20.215` should appear (or add manually). Click it — Moonlight shows a 4-digit PIN.
3. Open `https://192.168.20.215:47990` in a browser (accept the self-signed cert), log in with the Task 2 credentials, enter the PIN under PIN tab. Repeat for the second client.
4. Off-LAN: add the host by its Tailscale IP on one client and pair the same way.
5. In each Moonlight client's settings: resolution/refresh = that display's native mode, bitrate 80–150 Mbps on LAN (the spec's ceiling), ~20 Mbps for the Tailscale profile, HEVC on.

- [ ] **Step 2: Stream test — LAN**

USER STEP — stream "Desktop" from the Mac at the physical monitor's mode; then from the PC. Ask the user to confirm: video smooth, audio present, mouse/keyboard responsive, and stats overlay (Ctrl+Alt+Shift+S in Moonlight) shows decode+network under ~15 ms.

- [ ] **Step 3: Stream test — Tailscale**

USER STEP — same from off-LAN (or force the Tailscale IP host entry). Expect 1080p60 quality. Confirm it works, note measured bitrate/latency in this plan file.

- [ ] **Step 4: Desk-play regression**

USER STEP — confirm physical monitor + desk keyboard/mouse still work exactly as before (no streams active). Commit plan-file notes: `git commit -am "Record Phase 1 streaming results"`

---

### Task 5: Install the Virtual Display Driver

**Files:**
- Create: `scripts/gamemode/vdd-notes.md` (record of installed version + configured resolutions)

**Interfaces:**
- Produces: a VDD display adapter available (initially disabled) with the clients' native modes in its resolution list. Task 6 toggles it.

- [ ] **Step 1: Ask the user for client-native modes**

USER STEP — "What's the Mac's native resolution/refresh (e.g. 3456x2234@120) and the PC monitor's (e.g. 2560x1440@165)?" Record both.

- [ ] **Step 2: Install MTT Virtual Display Driver in the guest**

```bash
qm guest exec 200 --timeout 600 -- powershell -NoProfile -Command "\
  \$u = (Invoke-RestMethod https://api.github.com/repos/VirtualDisplay/Virtual-Display-Driver/releases/latest).assets \
    | Where-Object name -like '*.exe' | Select-Object -First 1 -ExpandProperty browser_download_url; \
  Invoke-WebRequest \$u -OutFile C:\\Windows\\Temp\\vdd-setup.exe; \
  Start-Process C:\\Windows\\Temp\\vdd-setup.exe -ArgumentList '/VERYSILENT /SUPPRESSMSGBOXES' -Wait"
```
Expected: `exitcode: 0`. Note: repo/installer naming drifts (project formerly `itsmikethetech/Virtual-Display-Driver`); if the silent flags differ for the current release, fall back to doing the install interactively via RDP and record what worked in `vdd-notes.md`.

- [ ] **Step 3: Add client resolutions to the VDD config**

Edit the VDD settings file (`C:\VirtualDisplayDriver\vdd_settings.xml` or the path the installer chose) via guest exec `Add-Content`/`Set-Content`, adding both modes from Step 1 plus 1920x1080@60. Restart the VDD (device disable/enable via `pnputil /disable-device` + `/enable-device`, or reboot the VM).

- [ ] **Step 4: Verify the display exists**

```bash
qm guest exec 200 --timeout 60 -- powershell -NoProfile -Command "Get-PnpDevice -Class Display | Select-Object FriendlyName,Status"
```
Expected: physical NVIDIA adapter + `Virtual Display Driver` (status OK), plus the emulated adapter. Commit: `git add scripts/gamemode/vdd-notes.md && git commit -m "Record VDD install and modes"`

---

### Task 6: Display switching with crash-safe restore (Phase 2 complete)

**Files:**
- Create: `scripts/gamemode/stream-on.ps1`, `scripts/gamemode/stream-off.ps1` (repo record; deployed to `C:\gamemode\` in guest)

**Interfaces:**
- Produces: Sunshine prep-cmd wiring: stream start → VDD-only at client mode; stream end (clean OR dirty) → physical monitor only. Restore-on-logon safety net.

- [ ] **Step 1: Deploy MultiMonitorTool + capture display configs**

In-guest: download `https://www.nirsoft.net/utils/multimonitortool-x64.zip` to `C:\gamemode\`, extract. Then, via RDP or desk session (USER STEP if guest exec is awkward for the interactive part): with the desk in its normal state run `C:\gamemode\MultiMonitorTool.exe /SaveConfig C:\gamemode\desk.cfg`; enable the VDD + disable physical in Windows display settings, run `/SaveConfig C:\gamemode\stream.cfg`; restore desk.

- [ ] **Step 2: Write the switch scripts**

`C:\gamemode\stream-on.ps1`:
```powershell
# Switch to VDD at the client's requested mode (Sunshine sets these env vars)
& C:\gamemode\MultiMonitorTool.exe /LoadConfig C:\gamemode\stream.cfg
Start-Sleep -Seconds 2
# Set VDD resolution to the connecting client's mode
& C:\gamemode\MultiMonitorTool.exe /SetMonitors "Name=\\.\DISPLAY_VDD Width=$env:SUNSHINE_CLIENT_WIDTH Height=$env:SUNSHINE_CLIENT_HEIGHT DisplayFrequency=$env:SUNSHINE_CLIENT_FPS"
```
`C:\gamemode\stream-off.ps1`:
```powershell
& C:\gamemode\MultiMonitorTool.exe /LoadConfig C:\gamemode\desk.cfg
```
Replace `DISPLAY_VDD` with the VDD's actual `\\.\DISPLAYn` name (from `MultiMonitorTool.exe /stext` output). Copy both into `scripts/gamemode/` in the repo.

- [ ] **Step 3: Wire Sunshine prep-cmds**

Via the Sunshine web UI (USER STEP) or by editing `sunshine.conf`: add a global prep command — do: `powershell -ExecutionPolicy Bypass -File C:\gamemode\stream-on.ps1`, undo: `powershell -ExecutionPolicy Bypass -File C:\gamemode\stream-off.ps1`. Also change `output_name` to the VDD output (Sunshine must capture the VDD during streams). Restart SunshineService.

- [ ] **Step 4: Restore-on-logon safety net**

```bash
qm guest exec 200 --timeout 60 -- powershell -NoProfile -Command "\
  Register-ScheduledTask -TaskName GameModeRestoreDisplay -Force \
    -Trigger (New-ScheduledTaskTrigger -AtLogOn) \
    -Action (New-ScheduledTaskAction -Execute powershell -Argument '-ExecutionPolicy Bypass -File C:\\gamemode\\stream-off.ps1')"
```
Expected: task registered. A reboot now always recovers the desk monitor.

- [ ] **Step 5: Test the ugly paths**

USER STEP checklist (confirm each):
1. Clean: start stream from Mac → VDD at Mac's mode (stats overlay shows client-native res); quit Moonlight → desk monitor restored.
2. Dirty: start stream, then `kill -9` Moonlight (or drop WiFi) → within Sunshine's session timeout, desk monitor restored (undo runs on session end, not just graceful quit).
3. Reboot mid-stream state: `qm guest exec 200 -- shutdown /r /t 0` while streaming → after boot + logon, desk monitor active.
4. Monitor asleep: turn off physical monitor, run a full stream cycle, wake monitor → desk restored.

- [ ] **Step 6: Commit**

`git add scripts/gamemode/stream-on.ps1 scripts/gamemode/stream-off.ps1 && git commit -m "Add game mode display switch scripts"`

---

### Task 7: `streaming_ready` + `gpu_home` in the status endpoint

**Files:**
- Modify: `/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/app.py:2566-2573` (the `status` branch of `vm_control`)

**Interfaces:**
- Consumes: existing `vm_control` route; `GPU_LOAN_FLAG` constant (app.py:97).
- Produces: `/api/vm/status` JSON gains `streaming_ready: bool` and `gpu_home: bool|null`. Task 8's tile logic reads exactly these two field names.

- [ ] **Step 1: Implement**

Replace the `status` branch body (app.py lines 2566-2573) with:

```python
        if action == "status":
            out = _sp.check_output(
                ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
                 "root@192.168.20.51", "qm status 200"],
                timeout=10, stderr=_sp.DEVNULL
            ).decode().strip()
            running = "running" in out
            streaming_ready = False
            gpu_home = None
            if running:
                # Sunshine answers 47984 only once Windows + the service are up.
                import socket as _socket
                try:
                    with _socket.create_connection(("192.168.20.215", 47984), timeout=1):
                        streaming_ready = True
                except OSError:
                    streaming_ready = False
            else:
                # Post-reclaim health: flag gone AND the 3080 visible in this CT.
                if os.path.exists(GPU_LOAN_FLAG):
                    gpu_home = False
                else:
                    try:
                        rc = _sp.run(["nvidia-smi", "-L"], capture_output=True,
                                     timeout=5).returncode
                        gpu_home = (rc == 0)
                    except Exception:
                        gpu_home = False
            return jsonify({"vm": "win11-gaming", "running": running, "raw": out,
                            "streaming_ready": streaming_ready, "gpu_home": gpu_home})
```

- [ ] **Step 2: Restart the service**

Run: `pct exec 101 -- systemctl restart ares && sleep 5 && pct exec 101 -- systemctl is-active ares`
Expected: `active`.

- [ ] **Step 3: Verify — VM stopped**

Run (from the host; grab a session cookie from a logged-in browser, or run inside the CT bypassing auth is not possible — use the cookie): `curl -sk -X POST -H "Cookie: <authed-cookie>" https://127.0.0.1/api/vm/status` against the dashboard's listen address inside CT 101 (`pct exec 101 -- curl -sk -X POST http://127.0.0.1:<app-port>/api/vm/status -H "Cookie: …"`).
Expected: `"running": false, "streaming_ready": false, "gpu_home": true`.

- [ ] **Step 4: Verify — VM running**

Start the VM (detached), wait for boot, repeat the curl.
Expected: `"running": true, "gpu_home": null`, and `streaming_ready` flips false→true as Sunshine comes up (~60-90 s after start). Stop the VM (detached qm stop is fine here), wait, confirm `gpu_home: true` again.

- [ ] **Step 5: Commit**

`git add app.py && git commit -m "Add streaming_ready and gpu_home to VM status"`

---

### Task 8: 4-state WIN tile (Phase 3 complete)

**Files:**
- Modify: `/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/templates/home.html` — `winTile()` (~line 864), the status poll in `loadVitals()` (~line 929)

**Interfaces:**
- Consumes: `streaming_ready`, `gpu_home` from Task 7 (exact names).
- Produces: tile states — Stopped/GPU home (idle), GPU MISSING (error, red), Starting/Stopping (existing busy pulse), Booting Windows (running, not ready), Ready to stream (green).

- [ ] **Step 1: Track the new fields**

In `loadVitals()`'s status fetch (~line 929), extend the state vars and re-render on any change:

```javascript
// near line 861:
var winRunning = false, winBusy = false, winReady = false, winGpuHome = true;

// in the .then() around line 931, replace the body:
.then(function(wd){
    var changed = (wd.running !== winRunning) ||
                  (!!wd.streaming_ready !== winReady) ||
                  ((wd.gpu_home !== false) !== winGpuHome);
    winRunning = wd.running;
    winReady = !!wd.streaming_ready;
    winGpuHome = (wd.gpu_home !== false);   // null (running) counts as fine
    if (changed) {
        var vh2 = document.getElementById('vitals-hero');
        if (vh2) updateWin(vh2, winTile());
    }
})
```

- [ ] **Step 2: New tile states in `winTile()`**

Insert before the `if (winRunning)` block:

```javascript
    if (!winRunning && !winGpuHome) {
        return '<div class="v-tile v-win" style="--accent-color:#ef4444;cursor:pointer" onclick="loadVitals()" title="GPU did not return to the gallery — check /var/log/gpu-swap.log">' +
            '<div class="v-tile-key"><span>WIN</span><span class="v-tile-tag">VM 200</span></div>' +
            '<div class="v-tile-num" style="font-size:20px;color:#ef4444">GPU MISSING</div>' +
            '<div class="v-tile-meta"><span class="lo">reclaim failed — see gpu-swap.log</span><span style="color:#ef4444">●</span></div>' +
        '</div>';
    }
```

And inside the `if (winRunning)` block, before the existing split-tile return:

```javascript
        if (!winReady) {
            return '<div class="v-tile v-win" style="--accent-color:#f59e0b">' +
                '<div class="v-tile-key"><span>WIN</span><span class="v-tile-tag">VM 200</span></div>' +
                '<div class="v-tile-num" style="font-size:20px;color:#f59e0b">Booting Windows</div>' +
                '<div class="v-tile-bar"><div class="v-tile-fill" style="width:75%;background:#f59e0b;animation:win-pulse 1.2s ease-in-out infinite"></div></div>' +
                '<div class="v-tile-meta"><span class="lo">waiting for Sunshine…</span><span style="color:#f59e0b">●</span></div>' +
            '</div>';
        }
```

In the existing running split-tile, change the status half's label from `Running` to `Ready to stream` (keep font small enough: add `style="font-size:18px;color:#10b981"` on the `v-tile-num`), and the stopped tile's meta from `click to start` to `click to start · GPU home`.

- [ ] **Step 3: Restart + eyeball**

Run: `pct exec 101 -- systemctl restart ares`. Open the dashboard: stopped state shows "GPU home". Start the VM from the tile: Starting → Booting Windows → Ready to stream. Stop it: Stopping → Stopped/GPU home.

- [ ] **Step 4: Error-state check (simulated)**

Simulate a failed reclaim: `touch /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/.gpu-on-loan` while the VM is stopped, wait one poll (15 s) → tile shows red GPU MISSING. Remove the flag: `rm /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/.gpu-on-loan` → back to "GPU home". (The flag is safe to touch/rm while the VM is stopped; the app only reads it at startup for CUDA and per-request here.)

- [ ] **Step 5: Commit**

`git add templates/home.html && git commit -m "Add 4-state WIN tile with streaming readiness"`

---

### Task 9: Idle watchdog (ONLY if Task 1 recorded PASS)

**Files:**
- Create: `scripts/gamemode/idle-watchdog.ps1` (repo record; deployed to `C:\gamemode\` in guest)

**Interfaces:**
- Consumes: gate-test PASS from Task 1; autologon (user step below).
- Produces: unattended VM shuts itself down after true idle; GPU auto-returns.

- [ ] **Step 1: Autologon (user step)**

USER STEP — print: "The watchdog must run in the interactive session (GetLastInputInfo is garbage from SYSTEM), which needs autologon. In the VM, run Sysinternals Autologon (https://live.sysinternals.com/Autologon.exe), enter your account + password, Enable. Say done when set." Note: with autologon, anyone at the physical console gets a session — acceptable on a home box behind a locked door; the lock screen after idle still applies if configured.

- [ ] **Step 2: Write the watchdog script**

`C:\gamemode\idle-watchdog.ps1` (also saved to `scripts/gamemode/idle-watchdog.ps1`):

```powershell
# Game-mode idle watchdog: shut down only when truly idle.
# All three must hold: no Sunshine session, input idle > 30 min, no busy GPU/network.
$ErrorActionPreference = 'SilentlyContinue'

# 1. Active stream? Moonlight holds TCP sessions on Sunshine ports.
$sunPorts = @(47984, 47989, 48010)
$stream = Get-NetTCPConnection -State Established |
    Where-Object { $sunPorts -contains $_.LocalPort }
if ($stream) { exit 0 }

# 2. Input idle (interactive-session-only API).
Add-Type @'
using System; using System.Runtime.InteropServices;
public static class Idle {
  [StructLayout(LayoutKind.Sequential)] struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }
  [DllImport("user32.dll")] static extern bool GetLastInputInfo(ref LASTINPUTINFO plii);
  public static uint Minutes() {
    var l = new LASTINPUTINFO(); l.cbSize = (uint)Marshal.SizeOf(typeof(LASTINPUTINFO));
    GetLastInputInfo(ref l);
    return ((uint)Environment.TickCount - l.dwTime) / 60000u;
  }
}
'@
if ([Idle]::Minutes() -lt 30) { exit 0 }

# 3. Foreground work? Downloads/updates/shader compiles must survive.
$netBps = (Get-Counter '\Network Interface(*)\Bytes Total/sec').CounterSamples |
    Measure-Object CookedValue -Sum | Select-Object -ExpandProperty Sum
if ($netBps -gt 1MB) { exit 0 }
$gpuUtil = & 'C:\Windows\System32\nvidia-smi.exe' --query-gpu=utilization.gpu --format=csv,noheader,nounits
if ([int]$gpuUtil -gt 10) { exit 0 }

# Truly idle: clean shutdown (never force). 5-min grace, cancellable with: shutdown /a
shutdown /s /t 300 /c "Game mode idle - shutting down in 5 min (shutdown /a to cancel)"
```

Deploy: write the file into the guest via `qm guest exec … Set-Content` (or copy-paste over RDP), path `C:\gamemode\idle-watchdog.ps1`.

- [ ] **Step 3: Register the scheduled task (interactive session, not SYSTEM)**

```bash
qm guest exec 200 --timeout 60 -- powershell -NoProfile -Command "\
  \$a = New-ScheduledTaskAction -Execute powershell -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\\gamemode\\idle-watchdog.ps1'; \
  \$t = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5); \
  Register-ScheduledTask -TaskName GameModeIdleWatchdog -Action \$a -Trigger \$t -Force"
```
Expected: task registered running as the logged-on user (default when registered this way from their context — verify with `Get-ScheduledTask GameModeIdleWatchdog | Select -Expand Principal`; if it shows SYSTEM, re-register with `-User <account>` and interactive logon type).

- [ ] **Step 4: Negative tests (nothing dies that shouldn't)**

With the VM up: (a) start a large Steam download, no input, wait 40 min → still running (network guard). (b) Start a Moonlight stream, idle hands 40 min → still running (session guard). (c) Desk session with occasional input → still running.

- [ ] **Step 5: Positive test (the whole loop)**

Leave the VM truly idle 35+ min → `shutdown /s /t 300` fires → VM stops → hookscript post-stop (proven in Task 1) → dashboard tile shows "Stopped · GPU home" and `pct exec 101 -- nvidia-smi -L` lists the 3080. Then run the full cycle a second time back-to-back (wedge regression).

- [ ] **Step 6: Commit**

`git add scripts/gamemode/idle-watchdog.ps1 && git commit -m "Add idle watchdog for game mode"`

---

## Gate test result

**PASS** (2026-07-10 16:58). `shutdown /s` from inside Windows (via `qm guest exec`) fired the hookscript post-stop through qmeventd: log shows the full release→rebind sequence at 16:58:07-14, `.gpu-on-loan` removed, `nvidia-smi -L` in CT 101 lists the 3080. Task 9 is GO.
