# Serves the temp dir (status JSON + dashboard) on :8000, like `python3 -m http.server 8000` in /tmp on macOS.
param([int]$Port = 8000)
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "Port $Port already in use - server (or something else) is already running."; return
}
Copy-Item (Join-Path (Split-Path -Parent $PSScriptRoot) "registration_report.html") $env:TEMP -Force
Write-Host "Dashboard: http://localhost:$Port/registration_report.html"
uv run python -m http.server $Port --directory $env:TEMP
