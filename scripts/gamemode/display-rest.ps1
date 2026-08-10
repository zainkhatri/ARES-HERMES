# Self-heal: ensure a GPU-backed display is ALWAYS active for headless remote
# streaming. When the physical OLED is asleep (remote use), the VDD must be the
# active primary or Windows falls back to the software "WinDisc 1024x768" display
# — which has no GPU/DirectX, so games (Rocket League) refuse to launch and apps
# render invisibly. Keep VDD enabled + primary at 2560x1440.
Enable-PnpDevice -InstanceId 'ROOT\DISPLAY\0001' -Confirm:$false -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3
$mmt = 'C:\gamemode\mmt\MultiMonitorTool.exe'
if (Test-Path $mmt) {
    & $mmt /enable MTT1337
    Start-Sleep -Milliseconds 800
    & $mmt /SetPrimary MTT1337
    Start-Sleep -Milliseconds 500
    & $mmt /SetMonitors "Name=MTT1337 Width=2560 Height=1440 DisplayFrequency=120"
}
exit 0
