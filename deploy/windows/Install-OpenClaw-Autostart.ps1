#requires -version 5.1
<#
.SYNOPSIS
  Register the OpenClaw gateway to start automatically when you log in
  to your Windows desk PC.

.DESCRIPTION
  Creates a Scheduled Task named "OpenClaw Gateway" that runs at user
  logon, restarts on failure, and survives reboots. The robot's host-
  presence monitor TCP-probes this gateway every 10 s — when it's up,
  Clawson wakes up; when it's down, Clawson goes to sleep pose.

  Idempotent: re-running replaces the existing task with the latest
  configuration.

.PARAMETER OpenClawCommand
  Full command line that launches the gateway. Adjust the executable
  path to match where you installed OpenClaw (CLI binary or batch
  wrapper). Common locations:
    - C:\Users\<you>\AppData\Local\Programs\openclaw\openclaw.exe
    - C:\Program Files\OpenClaw\openclaw.exe

.PARAMETER GatewayArgs
  Arguments passed to the OpenClaw binary. Default opens the gateway
  in serve mode on the bound port.

.EXAMPLE
  # From an Administrator PowerShell prompt:
  .\Install-OpenClaw-Autostart.ps1 -OpenClawCommand "C:\Users\louis\AppData\Local\Programs\openclaw\openclaw.exe"

.EXAMPLE
  # Custom args:
  .\Install-OpenClaw-Autostart.ps1 `
      -OpenClawCommand "C:\tools\openclaw.exe" `
      -GatewayArgs "agent --serve --port 18789"

.NOTES
  Run from an elevated PowerShell (right-click -> Run as Administrator).
  After install, verify with: Get-ScheduledTask -TaskName "OpenClaw Gateway"
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [string]$OpenClawCommand,

    [Parameter(Mandatory=$false)]
    [string]$GatewayArgs = "agent --serve",

    [Parameter(Mandatory=$false)]
    [string]$TaskName = "OpenClaw Gateway"
)

$ErrorActionPreference = "Stop"

# 1. Sanity-check the binary path.
if (-not (Test-Path $OpenClawCommand)) {
    Write-Error "OpenClawCommand not found: $OpenClawCommand"
    exit 1
}

# 2. Build the action.
$action = New-ScheduledTaskAction `
    -Execute $OpenClawCommand `
    -Argument $GatewayArgs

# 3. Trigger: at any user logon. Survives reboots.
$trigger = New-ScheduledTaskTrigger -AtLogOn

# 4. Settings: restart on crash up to 5 times, 1-minute backoff. Don't
#    pop a console window at logon.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1)

# 5. Run as the invoking user, in interactive context (so it can use
#    your Tailscale identity / firewall rules). LIMITED -> non-elevated.
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

# 6. Replace any existing task with the same name.
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Removing existing task '$TaskName'..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# 7. Register.
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Starts the OpenClaw gateway at logon so the Reachy Mini robot can connect to its tool backend." | Out-Null

Write-Host ""
Write-Host "Installed scheduled task: $TaskName" -ForegroundColor Green
Write-Host "  Command: $OpenClawCommand $GatewayArgs"
Write-Host "  Trigger: at logon (any user)"
Write-Host ""
Write-Host "Start it now without logging out:"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host ""
Write-Host "Verify:"
Write-Host "  Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Host ""
Write-Host "Next: get the robot to point at this PC."
Write-Host "  1. Find this machine's Tailscale name:  tailscale status"
Write-Host "  2. SSH to robot, edit ~/clawbody/.env:"
Write-Host "       OPENCLAW_GATEWAY_URL=ws://<this-machine>.tail<your-net>.ts.net:18789"
Write-Host "  3. Restart Clawson:                       sudo systemctl restart clawson"
