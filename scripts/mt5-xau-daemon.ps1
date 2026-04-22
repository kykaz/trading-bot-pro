param(
    [ValidateSet("run", "start", "stop", "status")]
    [string]$Action = "status",
    [int]$IntervalSeconds = 60
)

$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = (Resolve-Path (Join-Path $scriptRoot "..")).Path
$varDir = Join-Path $projectRoot "var"
$pidPath = Join-Path $varDir "mt5-xau-daemon.pid"
$logPath = Join-Path $varDir "mt5-xau-daemon.log"
$runnerPath = Join-Path $scriptRoot "trading-bot-venv.cmd"

if (-not (Test-Path $varDir)) {
    New-Item -ItemType Directory -Path $varDir | Out-Null
}

function Write-Log {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logPath -Value "[$timestamp] $Message"
}

function Get-DaemonProcess {
    if (-not (Test-Path $pidPath)) {
        return $null
    }

    $raw = (Get-Content $pidPath -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
    if (-not $raw) {
        return $null
    }

    try {
        $pidValue = [int]$raw
    }
    catch {
        return $null
    }

    try {
        return Get-Process -Id $pidValue -ErrorAction Stop
    }
    catch {
        return $null
    }
}

switch ($Action) {
    "start" {
        $existing = Get-DaemonProcess
        if ($null -ne $existing) {
            Write-Output "status=already_running"
            Write-Output "pid=$($existing.Id)"
            Write-Output "log=$logPath"
            exit 0
        }

        $escapedScript = $MyInvocation.MyCommand.Path.Replace('"', '""')
        $args = @(
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", """$escapedScript""",
            "-Action", "run",
            "-IntervalSeconds", "$IntervalSeconds"
        )
        $process = Start-Process -FilePath "powershell.exe" -ArgumentList $args -WindowStyle Minimized -PassThru
        Set-Content -Path $pidPath -Value $process.Id
        Write-Output "status=started"
        Write-Output "pid=$($process.Id)"
        Write-Output "interval_seconds=$IntervalSeconds"
        Write-Output "log=$logPath"
        exit 0
    }

    "run" {
        Set-Content -Path $pidPath -Value $PID
        Write-Log "daemon_started pid=$PID interval_seconds=$IntervalSeconds"
        while ($true) {
            try {
                Write-Log "cycle_start"
                $output = & $runnerPath run-once --source mt5 --live 2>&1 | Out-String
                Add-Content -Path $logPath -Value $output.TrimEnd()
                Write-Log "cycle_end"
            }
            catch {
                Write-Log "cycle_error=$($_.Exception.Message)"
            }
            Start-Sleep -Seconds $IntervalSeconds
        }
    }

    "stop" {
        $existing = Get-DaemonProcess
        if ($null -eq $existing) {
            if (Test-Path $pidPath) {
                Remove-Item -LiteralPath $pidPath -Force
            }
            Write-Output "status=not_running"
            exit 0
        }

        Stop-Process -Id $existing.Id -Force
        Remove-Item -LiteralPath $pidPath -Force
        Write-Output "status=stopped"
        Write-Output "pid=$($existing.Id)"
        Write-Output "log=$logPath"
        exit 0
    }

    "status" {
        $existing = Get-DaemonProcess
        if ($null -eq $existing) {
            Write-Output "status=not_running"
            Write-Output "log=$logPath"
            exit 0
        }

        Write-Output "status=running"
        Write-Output "pid=$($existing.Id)"
        Write-Output "log=$logPath"
        exit 0
    }
}
