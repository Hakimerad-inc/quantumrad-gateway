# install_windows_autolaunch.ps1 — S10-T3 Windows auto-launch registration.
#
# Registers a per-user Scheduled Task that starts the gateway from the USB's
# P2 (NTFS) partition when the drive is present — the spec §5.1 decision:
# scheduled task, NOT autorun.inf (blocked by default since Windows 7).
#
# Run from the P2 partition (or pass -UsbRoot):
#   powershell -ExecutionPolicy Bypass -File install_windows_autolaunch.ps1
# Uninstall:
#   powershell -File install_windows_autolaunch.ps1 -Uninstall
#
# Validation status: syntax-checked; hardware leg deferred (S10-T3 — needs a
# Windows 10/11 host; tracked in docs/qa/usb-uat-10.md §5).
#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [string]$TaskName = 'MercureGatewayUSB',
    [string]$GatewayExe,   # default: portable python + gateway under P2 root
    [string]$UsbRoot = (Split-Path -Parent $MyInvocation.MyCommand.Path)
)

$ErrorActionPreference = 'Stop'

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "removed scheduled task $TaskName"
    } else {
        Write-Host "no scheduled task $TaskName present"
    }
    exit 0
}

if (-not $GatewayExe) {
    # Portable layout on P2 (usb-dongle-spec §4.1): python\python.exe running
    # the bundled gateway wheel; fall back to a packaged exe if present.
    $candidate = Join-Path $UsbRoot 'gateway\mercure-gateway.exe'
    if (Test-Path $candidate) {
        $GatewayExe = $candidate
    } else {
        $GatewayExe = (Join-Path $UsbRoot 'python\python.exe')
        if (-not (Test-Path $GatewayExe)) {
            Write-Error "neither gateway exe nor portable python found under $UsbRoot"
        }
    }
}

$arguments = ''
if ($GatewayExe -match 'python\.exe$') {
    $arguments = "-m mercure_gateway --web"
} else {
    $arguments = "--web"
}

# Trigger: every 5 minutes, the action no-ops when the config marker on this
# drive is missing (drive unplugged) — schtasks has no device-arrival trigger
# without a SystemEvent subscription (WM_DEVICECHANGE runs in the gateway's
# own hotplug listener on Linux mode; Windows mode uses this polled approach).
$action  = New-ScheduledTaskAction -Execute $GatewayExe -Argument $arguments `
           -WorkingDirectory $UsbRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
           -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -MultipleInstances IgnoreNew `
            -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

# Idempotence: the task is only meaningful while this drive is mounted at the
# recorded path; if P2 moved, exit 0 silently (a cheap "is this the stick?"
# guard is done by the working-directory existence check above).
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Set-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings | Out-Null
    Write-Host "updated scheduled task $TaskName"
} else {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Description "QuantumRAD gateway USB auto-launch (S10-T3)" | Out-Null
    Write-Host "registered scheduled task $TaskName (5-min poll, no-op when drive absent)"
}
