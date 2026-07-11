# Sunshine global prep command (do). Runs in the user session before every
# stream launch; non-zero exit makes Sunshine abort the launch.
# Blocks streaming while someone is actively at the desk (console input < 120s),
# unless C:\gamemode\force-stream.flag exists and is fresh (< 10 min).
$flag = 'C:\gamemode\force-stream.flag'
if (Test-Path $flag) {
    $age = (Get-Date) - (Get-Item $flag).LastWriteTime
    if ($age.TotalMinutes -lt 10) { exit 0 }
    Remove-Item $flag -Force -ErrorAction SilentlyContinue
}
Add-Type @'
using System; using System.Runtime.InteropServices;
public static class Idle {
  [StructLayout(LayoutKind.Sequential)] struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }
  [DllImport("user32.dll")] static extern bool GetLastInputInfo(ref LASTINPUTINFO plii);
  public static uint Seconds() {
    var l = new LASTINPUTINFO(); l.cbSize = (uint)Marshal.SizeOf(typeof(LASTINPUTINFO));
    GetLastInputInfo(ref l);
    return ((uint)Environment.TickCount - l.dwTime) / 1000u;
  }
}
'@
# ponytail: 120s threshold is a guess; tune if desk handoff feels slow
if ([Idle]::Seconds() -lt 120) { exit 1 }
exit 0
