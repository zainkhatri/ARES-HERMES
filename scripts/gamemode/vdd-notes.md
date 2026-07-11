# VDD install record — VM 200 (2026-07-10)

- **Driver:** VirtualDrivers/Virtual-Display-Driver 25.7.23, x86 driver-only zip → `C:\VirtualDisplayDriver\`
- **Install method:** signer cert (Valid, extracted from MttVDD.dll) added to Root + TrustedPublisher; devnode created with nefcon (`nefconc --create-device-node --hardware-id Root\MttVDD --class-name Display --class-guid 4d36e968-…`); `pnputil /add-driver MttVDD.inf /install` → ROOT\DISPLAY\0001. Tools kept in `C:\gamemode\`.
- **vdd_settings.xml:** gpu friendlyname = `NVIDIA GeForce RTX 3080`; added 3024x1964 (Mac 14" XDR native) to resolutions; global refresh rates incl. 120 already present. Monitor shows as **"VDD by MTT"**, Sunshine sees `\\.\DISPLAY21`, device_id `{5eb52002-659f-5729-bdd8-9cdc4efd1bf5}`.
- **Display switching: NO MultiMonitorTool / prep-cmd scripts.** Sunshine's built-in display-device management replaced the planned script stack (`sunshine.conf`): `output_name = {5eb52002…}`, `dd_configuration_option = ensure_only_display`, `dd_resolution_option = auto`, `dd_refresh_rate_option = auto`. Sunshine switches to the VDD at the client's exact mode on stream start, restores the previous display topology on end, and persists state to recover after crashes.
- **Why the physical monitor mattered:** with the desk monitor asleep the 3080 had ZERO outputs (`WmiMonitorID` count 0, Windows on 1024x768 WinDisc fallback) → Sunshine streamed black. The VDD fixes headless streaming permanently.
- **Found in the VM:** a leftover **Parsec Virtual Display Adapter** (Parsec previously installed). Harmless but consider uninstalling Parsec to reduce the adapter zoo (4 display adapters currently: 3080, VDD, Parsec-VDA, Microsoft Basic/vga-std).
- PC-monitor native mode not yet added to vdd_settings.xml — add a `<resolution>` block when the second client's mode is known (Sunshine dd_resolution auto still needs the mode to exist in the VDD list, or it falls back to nearest).

**2026-07-10 late:** post-reboot the VDD came up at 800x600@30 (first XML entry) → "zoomed in" stream. Removed all sub-1920 modes from vdd_settings.xml (now: 1920x1080, 2560x1440, 3840x2160, 3024x1964). Note: display mode changes via guest-exec run in session 0 and do nothing — use the interactive scheduled-task trampoline (C:\gamemode\cursor-trace.ps1 + Start-ScheduledTask CursorTrace).

**2026-07-11 — display saga resolved, VDD pinning REVERTED.**
Root causes found the hard way:
1. `output_name = {VDD-guid}` makes Sunshine CRASH ON STARTUP whenever the VDD isn't an active display path (i.e. any time it's disabled or the OLED is sole) — "Device does not exist in available path source data".
2. A multiline `global_prep_cmd` (embedded JSON value split across two conf lines) → `boost json_parser_error: expected value` → crash-loop that no config edit could fix because it died before reading config.
3. Trying to disable the VDD at rest ALSO breaks #1.

**Fix / current known-good config (sunshine.conf):** only `global_prep_cmd = [{...desk-guard...}]` on a SINGLE line. NO `output_name` pin, NO `dd_configuration_option`. Sunshine captures the PRIMARY display. Resting topology = OLED (Odyssey G60SD) primary @ native 2560x1440, VDD present as ignored 800x600 secondary. Streaming = OLED mirror @ 1440p; desk never blacks out; Sunshine starts reliably.
**Tradeoff accepted:** lost pixel-perfect Mac-native (3024x1964) streaming. To restore later WITH Zain watching the screen: set a clean EXTEND topology, re-pin output_name to the VDD, and verify Sunshine starts — never disable the VDD while output_name points at it.
Recovery that unwedged it: reinstall Sunshine /S over the top (pairing in sunshine_state.json survives), then single-line guard conf.

**2026-07-11 — BULLETPROOF FINAL: mirror-the-OLED model (VDD auto-juggle abandoned).**
Decision (Zain): stop auto-switching OLED↔VDD — it broke every attempt because it drives an unseen physical monitor. Final architecture:
- **VDD disabled at device level** (`Disable-PnpDevice ROOT\DISPLAY\0001`). It kept stealing primary at 3024x1964 on non-deterministic boots, blacking the OLED. Disabled = OLED is always sole primary.
- **sunshine.conf** = ONLY `global_prep_cmd = [{desk-guard}]` (single line). Empty output_name → captures primary = OLED @ native 2560x1440. Stream is 1440p (not Mac-native 3024x1964 — accepted tradeoff for zero fragility).
- **Deleted `config\display_device.state`** — Sunshine persisted the old VDD topology here and replayed it on every connect ("Failed to change topology to {VDD-guid}", ~2s delay). Gone → clean instant capture, no topology errors.
- **Self-heal:** `GameModeDisplayRest` scheduled task (AtLogOn, wscript-hidden, runs display-rest.ps1) re-disables the VDD + pins OLED native every boot — kills the boot-race where VDD grabbed primary.
- **Service recovery:** SunshineService set to auto-restart on failure (sc failure restart/5s x2 then 10s).
- Verified end-to-end via Mac SSH: guard blocks when desk-hot (status -1), stream succeeds with force-flag, log shows clean `Desktop resolution [2560x1440]` + NvEnc, no topology errors, OLED never blacks (nothing switches).
- **No-restore-needed win:** with zero display switching, the "dirty disconnect leaves desk black" failure mode is structurally impossible now.
Display IDs: OLED=SAM75CB (Odyssey G60SD), VDD=MTT1337. Tool: MultiMonitorTool in C:\gamemode\mmt. To revive pixel-perfect later, that's a MANUAL desk-mode/stream-mode toggle done with Zain watching — never an auto prep-cmd.
