# Game-mode idle watchdog: shut down only when truly idle.
# Deployed to C:\gamemode\idle-watchdog.ps1 in VM 200; scheduled task runs it
# every 5 min in the interactive session (autologon enabled).
# All three must hold: no Sunshine session, input idle > 30 min, no busy GPU/network.
$ErrorActionPreference = 'SilentlyContinue'

# 1. Active stream? A live Moonlight session = an active NVENC encoder session.
#    (Established TCP is unreliable — Moonlight rides UDP once running.)
$encSessions = & "$env:windir\System32\nvidia-smi.exe" --query-gpu=encoder.stats.sessionCount --format=csv,noheader,nounits
if ([int]$encSessions -gt 0) { exit 0 }

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
$netBps = ((Get-Counter '\Network Interface(*)\Bytes Total/sec').CounterSamples |
    Measure-Object CookedValue -Sum).Sum
if ($netBps -gt 1MB) { exit 0 }
$gpuUtil = & "$env:windir\System32\nvidia-smi.exe" --query-gpu=utilization.gpu --format=csv,noheader,nounits
if ([int]$gpuUtil -gt 10) { exit 0 }

# Truly idle: clean shutdown (never force). 5-min grace, cancellable with: shutdown /a
shutdown /s /t 300 /c "Game mode idle - shutting down in 5 min (shutdown /a to cancel)"
