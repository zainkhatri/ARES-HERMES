' Invisible launcher for the idle watchdog — wscript spawns no console window.
' Scheduled task GameModeIdleWatchdog runs: wscript.exe C:\gamemode\launcher.vbs
CreateObject("Wscript.Shell").Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\gamemode\idle-watchdog.ps1", 0, False
