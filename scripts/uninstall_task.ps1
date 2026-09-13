param([string]$TaskName = "NFL Odds Tracker")
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Tarea '$TaskName' eliminada."
