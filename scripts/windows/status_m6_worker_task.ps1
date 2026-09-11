<#
.SYNOPSIS
Read-only status of the M6-06 Windows PC Worker Task Scheduler task.

.DESCRIPTION
Displays task existence, state, last run, last result, and next run time.
Read-only: never registers/unregisters/modifies any task. Does not expose
config secrets.

.PARAMETER TaskName
Task Scheduler task name to inspect. Default: PkpM6WindowsWorker.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "PkpM6WindowsWorker"
)

$ErrorActionPreference = "Stop"

$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $Task) {
    Write-Host "Task '$TaskName': NOT INSTALLED"
    Write-Host "(M6-06 ships ready-to-install autostart artifacts; production registration is deferred to M6-08.)"
    exit 0
}

Write-Host "Task:              $TaskName"
Write-Host "State:             $($Task.State)"

$Info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "LastRunTime:       $($Info.LastRunTime)"
Write-Host "LastTaskResult:    $($Info.LastTaskResult)"
Write-Host "NextRunTime:       $($Info.NextRunTime)"
Write-Host "NumberOfMissedRuns: $($Info.NumberOfMissedRuns)"

Write-Host ""
Write-Host "Actions (secret-free):"
foreach ($action in $Task.Actions) {
    Write-Host "  Execute:  $($action.Execute)"
    Write-Host "  Args:     $($action.Arguments)"
    Write-Host "  WorkDir:  $($action.WorkingDirectory)"
}
exit 0