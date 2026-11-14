# Registers a Windows scheduled task that runs the relay at logon and
# restarts it if the process exits.
# Run once from PowerShell:  powershell -ExecutionPolicy Bypass -File packaging\install-windows-task.ps1
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Run start_live_relay.bat once to create .venv before installing the task."
}

$Action = New-ScheduledTaskAction -Execute $Python -Argument "-m live_relay --service --dashboard-port 8765" -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask `
    -TaskName "Live Relay" `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Live Relay — 24/7 broadcast relay, dashboard and post-stream automation" `
    -Force | Out-Null

Write-Host "Registered the 'Live Relay' task. Start it from Task Scheduler or reboot."
