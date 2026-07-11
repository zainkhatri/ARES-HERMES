# Sunshine + ViGEmBus install for VM 200 (win11-gaming) — executed 2026-07-10
# via: qm guest exec 200 --timeout 600 -- powershell -NoProfile -Command "<block>"
# VM static IP: 192.168.20.215 (set the same day; DHCP disabled on Ethernet)

$ProgressPreference = 'SilentlyContinue'

# Sunshine (latest release, silent NSIS install -> SunshineService, autostart)
$u = (Invoke-RestMethod https://api.github.com/repos/LizardByte/Sunshine/releases/latest).assets |
    Where-Object name -like '*windows-amd64-installer.exe' |
    Select-Object -First 1 -ExpandProperty browser_download_url
Invoke-WebRequest $u -OutFile C:\Windows\Temp\sunshine.exe
Start-Process C:\Windows\Temp\sunshine.exe -ArgumentList '/S' -Wait

# ViGEmBus (gamepad support — Sunshine logs Fatal without it)
$v = (Invoke-RestMethod https://api.github.com/repos/nefarius/ViGEmBus/releases/latest).assets |
    Where-Object name -like '*x64*.exe' |
    Select-Object -First 1 -ExpandProperty browser_download_url
Invoke-WebRequest $v -OutFile C:\Windows\Temp\vigem.exe
Start-Process C:\Windows\Temp\vigem.exe -ArgumentList '/quiet','/norestart' -Wait
Restart-Service SunshineService

# Note: output_name NOT pinned in Phase 1 — display device list was empty
# (monitor asleep). Pinned to the VDD output in Phase 2 (stream-on/off scripts).
