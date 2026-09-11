<#
.SYNOPSIS
Run the M6-06 Windows PC Worker Host using the repository's exact venv Python.

.DESCRIPTION
Resolves the repository root from this script's location, chooses the exact
venv Python, and invokes the worker host. Propagates the worker host exit code
so Task Scheduler restart policy can react to it. This wrapper implements NO
worker logic - it only resolves and invokes.

.PARAMETER Config
Path to the worker host config JSON.

.PARAMETER Command
Worker host subcommand (run | preflight | print-config). Default: run.

.PARAMETER Json
Emit JSON output (preflight / print-config).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Config,

    [ValidateSet("run", "preflight", "print-config")]
    [string]$Command = "run",

    [switch]$Json
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent (Split-Path -Parent $ScriptDir)
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Error "venv Python not found: $VenvPython"
    exit 5
}

if (-not (Test-Path -LiteralPath $Config)) {
    Write-Error "config file not found: $Config"
    exit 2
}

$Arguments = @(
    "-m", "src.operations.windows_worker",
    $Command,
    "--config", $Config
)
if ($Json) { $Arguments += "--json" }

Push-Location $RepoRoot
try {
    & $VenvPython @Arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}