param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $projectRoot ".venv"

& $Python -m venv $venv
& (Join-Path $venv "Scripts\\python.exe") -m pip install --upgrade pip
& (Join-Path $venv "Scripts\\python.exe") -m pip install -r (Join-Path $projectRoot "requirements.txt")

Write-Host "Dependencies installed. Before the first run, verify config\\config.json and install/download your ASR model."
