# Registra la tarea programada que ejecuta el tracker cada 30 minutos.
# El propio tracker decide si toma snapshot: en punto siempre, y a la media hora
# solo si algún partido arranca en las próximas 2 horas.
param([string]$TaskName = "NFL Odds Tracker")
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = (Get-Command python -ErrorAction Stop).Source
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }

$now = Get-Date
$start = $now.Date.AddHours($now.Hour).AddMinutes([math]::Ceiling(($now.Minute + 1) / 30) * 30)

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m tracker run" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Minutes 30)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Snapshots de momios NFL y alertas de movimiento de linea" -Force | Out-Null

Write-Host "Tarea '$TaskName' registrada con $pythonw"
Write-Host "Primera ejecucion: $start (luego cada 30 minutos)"
