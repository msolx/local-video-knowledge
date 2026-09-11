<#
.SYNOPSIS
Uninstall the M6-06 Windows PC Worker Task Scheduler task (idempotent).

.DESCRIPTION
Removes the task if it exists. If the task does not exist, this is a normal
no-op (exit 0). Never touches any other task.

.PARAMETER TaskName
Task Scheduler task name to remove. Default: PkpM6WindowsWorker.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "PkpM6WindowsWorker"
)

$ErrorActionPreference = "Stop"

$Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $Existing) {
    Write-Host "Task '$TaskName' does not exist - nothing to uninstall (no-op)."
    exit 0
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Uninstalled task '$TaskName'."
exit 0