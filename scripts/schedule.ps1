# Registers a daily Windows scheduled task that runs the full offerscan pipeline.
# Usage (PowerShell): .\scripts\schedule.ps1 -Time "08:00" [-Engine jev] [-TaskName offerscan-daily]
param(
    [string]$Time = "08:00",
    [ValidateSet("jev", "heuristic")] [string]$Engine = "heuristic",
    [string]$TaskName = "offerscan-daily"
)

$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root ".venv\Scripts\offerscan.exe"
if (-not (Test-Path $exe)) { throw "No se encuentra $exe. Crea el venv e instala el proyecto primero." }

$action = New-ScheduledTaskAction -Execute $exe -Argument "run --engine $Engine" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Force | Out-Null
Write-Host "Tarea '$TaskName' registrada: todos los dias a las $Time (motor: $Engine)."
Write-Host "Para quitarla: Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
