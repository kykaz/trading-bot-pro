$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\\Scripts\\python.exe"
$script = Join-Path $projectRoot "scripts\\publish_public_snapshot.py"
$logPath = Join-Path $projectRoot "var\\publish-public-snapshot.log"

if (-not (Test-Path (Split-Path -Parent $logPath))) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logPath) | Out-Null
}

$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $logPath -Value "[$timestamp] publish_cycle_start"

try {
    & $python $script 2>&1 | Tee-Object -FilePath $logPath -Append
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logPath -Value "[$timestamp] publish_cycle_end"
}
catch {
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logPath -Value "[$timestamp] publish_cycle_error $($_.Exception.Message)"
    throw
}
