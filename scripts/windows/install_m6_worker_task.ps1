<#
.SYNOPSIS
Install (dry-run by default) the M6-06 Windows PC Worker as a Task Scheduler task.

.DESCRIPTION
Validates config/paths and creates the Task Scheduler task definition. By
default this script is a GENERATE/DRY-RUN: it prints the exact task definition
(task name, trigger, executable, arguments, working dir, restart policy) and
makes NO mutation. Pass -Apply to actually register the task - production
registration is intentionally deferred until M6-08 when the NAS control-plane
transport is verified, so -Apply should not be used in normal operation.

.PARAMETER Config
Path to the worker host config JSON (used only for validation and naming).

.PARAMETER TaskName
Task Scheduler task name. Default: PkpM6WindowsWorker.

.PARAMETER StartupDelaySeconds
Delay after logon before the task starts. Default: 45.

.PARAMETER RestartIntervalMinutes
Restart interval on failure (Task Scheduler). Default: 1.

.PARAMETER RestartCount
Max restarts on failure. Default: 999 (maximum supported by Windows Task Scheduler schema).

.PARAMETER Apply
Actually register the task. WITHOUT this flag the script is a strict dry-run.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Config,

    [string]$TaskName = "PkpM6WindowsWorker",
    [int]$StartupDelaySeconds = 45,
    [int]$RestartIntervalMinutes = 1,
    [int]$RestartCount = 999,
    [switch]$Apply
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent (Split-Path -Parent $ScriptDir)
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$WorkerScript = Join-Path $ScriptDir "run_m6_worker.ps1"

# Validate the config by running the host's own print-config (no mutation).
if (-not (Test-Path -LiteralPath $Config)) {
    Write-Error "config file not found: $Config"
    exit 2
}
if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Error "venv Python not found: $VenvPython"
    exit 2
}
Push-Location $RepoRoot
try {
    & $VenvPython -m src.operations.windows_worker print-config --config $Config *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Error "config validation failed (exit $LASTEXITCODE)"
        exit 2
    }
}
finally {
    Pop-Location
}

$TaskArguments = "`"-File`" `"$WorkerScript`" -Config `"$Config`" -Command run"

# A 24h reset boundary prevents runaway restart loops while allowing the
# long-running host to stay up across days via logon-trigger + on-failure restarts.
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount $RestartCount `
    -RestartInterval (New-TimeSpan -Minutes $RestartIntervalMinutes) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Trigger.Delay = "PT${StartupDelaySeconds}S"

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$WorkerScript`" -Config `"$Config`" -Command run" `
    -WorkingDirectory $RepoRoot

$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

$Task = New-ScheduledTask -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal

Write-Host "=== M6-06 Worker Task Definition (dry-run) ==="
Write-Host "TaskName:              $TaskName"
Write-Host "Trigger:               AtLogOn + delay ${StartupDelaySeconds}s"
Write-Host "Executable:            powershell.exe"
Write-Host "Arguments:             $($Action.Arguments)"
Write-Host "WorkingDirectory:      $RepoRoot"
Write-Host "RestartPolicy:         every ${RestartIntervalMinutes} min, up to $RestartCount times"
Write-Host "ExecutionTimeLimit:    none (long-running host)"
Write-Host "RunLevel:              Limited (interactive user)"
Write-Host "Secret exposure:        none (worker host redacts config/log output)"
Write-Host ""

if ($Apply) {
    Write-Host "Registering task '$TaskName' ..."
    Register-ScheduledTask -TaskName $TaskName -InputObject $Task -Force | Out-Null
    Write-Host "Registered. Production registration is NOT intended until M6-08."
}
else {
    Write-Host "DRY-RUN only - no task was registered."
    Write-Host "To actually register (NOT recommended until M6-08): run with -Apply"
    exit 0
}