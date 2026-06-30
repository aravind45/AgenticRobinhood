# setup_scheduler.ps1
# Registers a Windows Task Scheduler job that runs the Project Alpha daemon
# every weekday at 9:35 AM.
#
# Run once as Administrator in PowerShell:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\setup_scheduler.ps1

$taskName  = "ProjectAlpha-TradingDaemon"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$batFile   = Join-Path $scriptDir "run_alpha.bat"

Write-Host "=== Project Alpha - Task Scheduler Setup ===" -ForegroundColor Cyan
Write-Host "Script dir : $scriptDir"
Write-Host "Batch file : $batFile"

if (-not (Test-Path $batFile)) {
    Write-Error "run_alpha.bat not found at $batFile. Aborting."
    exit 1
}

# Remove existing task if present
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Write-Host "Removing existing task..."
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

# Action: run the batch file
$action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c `"$batFile`"" `
    -WorkingDirectory $scriptDir

# Trigger: weekdays at 9:35 AM local time
$trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At "09:35AM"

# Settings
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -MultipleInstances IgnoreNew

# Run as current user
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Highest

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Project Alpha autonomous SPY trading daemon" `
    -Force

Write-Host ""
Write-Host "=== Task registered successfully ===" -ForegroundColor Green
Write-Host "Task name : $taskName"
Write-Host "Runs at   : 9:35 AM every weekday"
Write-Host "Log output: $scriptDir\alpha_daemon.log"
Write-Host ""
Write-Host "To run now    : Start-ScheduledTask -TaskName '$taskName'"
Write-Host "To remove     : Unregister-ScheduledTask -TaskName '$taskName'"
Write-Host "To check logs : Get-Content '$scriptDir\alpha_daemon.log' -Tail 50"
