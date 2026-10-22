# Instala offerscan en este directorio (Windows).
# Uso: powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$PyExe = $null
$PyPrefix = @()
foreach ($cmd in @("py", "python", "python3")) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { continue }
    if ($cmd -eq "py") {
        & py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $PyExe = "py"; $PyPrefix = @("-3"); break }
    } else {
        & $cmd -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $PyExe = $cmd; $PyPrefix = @(); break }
    }
}
if (-not $PyExe) {
    Write-Host "No encuentro Python 3.10+. Instálalo desde https://www.python.org/downloads/ y marca 'Add python.exe to PATH'."
    exit 1
}

$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "Creando entorno .venv..."
    & $PyExe @PyPrefix -m venv .venv
}

# Windows locks a running .exe: reinstalling while `offerscan ui` is open breaks the install half-way.
$venvScripts = Join-Path $root ".venv\Scripts"
$running = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($venvScripts, [StringComparison]::OrdinalIgnoreCase) }
if ($running) {
    Write-Host "offerscan está abierto (por ejemplo 'offerscan ui'). Ciérralo con Ctrl+C en su terminal y vuelve a ejecutar este script."
    exit 1
}

& $venvPy -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { Write-Host "No se pudo actualizar pip. Revisa la conexión y repite."; exit 1 }
& $venvPy -m pip install -e ".[ui]"
if ($LASTEXITCODE -ne 0) {
    Write-Host "La instalación de offerscan falló (mira el error de arriba). Revisa la conexión y repite el script."
    exit 1
}

$envFile = Join-Path $root ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $root ".env.example") $envFile
    Write-Host "Creado .env (no hace falta rellenarlo para el uso básico)."
}

Write-Host "Instalando el navegador para los portales que bloquean (InfoJobs, Indeed, Glassdoor)..."
$offerscan = Join-Path $root ".venv\Scripts\offerscan.exe"
& $offerscan install-browser
$browserOk = ($LASTEXITCODE -eq 0)

Write-Host ""
if (-not $browserOk) {
    Write-Host "AVISO: el navegador no quedó listo. El resto funciona; para InfoJobs, Indeed y Glassdoor"
    Write-Host "repite más tarde:  .\.venv\Scripts\offerscan install-browser"
    Write-Host ""
}
Write-Host "Listo. Arranca la interfaz con:"
Write-Host "  .\.venv\Scripts\offerscan ui"
