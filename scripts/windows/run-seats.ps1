# Seat check + auto-reserve. Windows equivalent of ~/bin/awsevents_seats.sh (log rotates at 5 MB).
$ErrorActionPreference = "Continue"
$Log = Join-Path $env:TEMP "awsevents_cron.log"
if ((Test-Path $Log) -and ((Get-Item $Log).Length -gt 5MB)) {
    Move-Item -Force "$Log.1" "$Log.2" -ErrorAction SilentlyContinue
    Move-Item -Force $Log "$Log.1"
}
Set-Location (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
# ToString() unwraps stderr ErrorRecords so the log shows plain text, not NativeCommandError noise
uv run awsevents seats --auto-reserve 2>&1 | ForEach-Object { $_.ToString() } | Out-File -Append -Encoding utf8 $Log
