# Starts the local Laya decision server (free, CPU) from .venv-laya.
# Usage: .\scripts\laya.ps1 [-Install] [-Port 8000]
param(
    [switch]$Install,
    [int]$Port = 8000
)

$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root ".venv-laya"
$python = Join-Path $venv "Scripts\python.exe"

if ($Install) {
    if (-not (Test-Path $python)) { python -m venv $venv }
    & $python -m pip install --upgrade pip
    & $python -m pip install "laya[serve]"
    Write-Host "Laya instalado en $venv (la primera ejecucion descarga ~650 MB de modelo)."
    return
}

$serve = Join-Path $venv "Scripts\laya-serve.exe"
if (-not (Test-Path $serve)) { throw "No existe $serve. Ejecuta primero: .\scripts\laya.ps1 -Install" }

$env:LAYA_MODELS = "multilingual"
$env:LAYA_HOST = "127.0.0.1"
$env:LAYA_PORT = "$Port"
Write-Host "Laya escuchando en http://127.0.0.1:$Port (Ctrl+C para parar)"
& $serve
