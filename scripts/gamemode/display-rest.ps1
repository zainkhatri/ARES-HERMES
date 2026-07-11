# Self-heal: enforce the known-good desk display state at every logon.
# Keeps the VDD out of the way so it can never steal primary on boot again,
# and pins the OLED to native. Streaming uses empty output_name -> captures
# this primary. See vm200-sunshine-display memory.
$vdd = Get-PnpDevice -InstanceId 'ROOT\DISPLAY\0001' -ErrorAction SilentlyContinue
if ($vdd -and $vdd.Status -eq 'OK') {
    Disable-PnpDevice -InstanceId 'ROOT\DISPLAY\0001' -Confirm:$false -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
}
$mmt = 'C:\gamemode\mmt\MultiMonitorTool.exe'
if (Test-Path $mmt) {
    & $mmt /SetMonitors "Name=SAM75CB Width=2560 Height=1440 DisplayFrequency=360"
}
exit 0
