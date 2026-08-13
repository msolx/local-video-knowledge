param([string]$Python = "python")

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $projectRoot ".venv-f2"
& $Python -m venv $venv
& (Join-Path $venv "Scripts\\python.exe") -m pip install --upgrade pip
& (Join-Path $venv "Scripts\\python.exe") -m pip install -r (Join-Path $projectRoot "requirements-f2-poc.txt")
Write-Host "F2 POC environment ready: .venv-f2\\Scripts\\f2.exe"
