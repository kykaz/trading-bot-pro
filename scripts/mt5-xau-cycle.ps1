param()

$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = (Resolve-Path (Join-Path $scriptRoot "..")).Path
$varDir = Join-Path $projectRoot "var"
$logPath = Join-Path $varDir "mt5-xau-scheduled.log"
$runnerPath = Join-Path $scriptRoot "trading-bot-venv.cmd"

if (-not (Test-Path $varDir)) {
    New-Item -ItemType Directory -Path $varDir | Out-Null
}

function Write-Log {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logPath -Value "[$timestamp] $Message"
}

try {
    Write-Log "cycle_start"
    $output = & $runnerPath run-once --source mt5 --live 2>&1 | Out-String
    if ($output) {
        Add-Content -Path $logPath -Value $output.TrimEnd()
    }
    Write-Log "cycle_end"
    exit 0
}
catch {
    Write-Log "cycle_error=$($_.Exception.Message)"
    exit 1
}
