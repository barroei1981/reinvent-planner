# Registers a per-user Scheduled Task that runs run-seats.ps1 every 10 minutes.
# Runs in your interactive session (needed to read Chrome's cookies). Re-run safe; remove with -Remove.
param([switch]$Remove, [int]$EveryMinutes = 10)
$Name = "awsevents-seats"
if ($Remove) { Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue; return }
$Script = Join-Path $PSScriptRoot "run-seats.ps1"
$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Script`""
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 8)
Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
Start-ScheduledTask -TaskName $Name
Write-Host "Task '$Name' registered (every $EveryMinutes min) and started. Log: $env:TEMP\awsevents_cron.log"
