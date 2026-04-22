$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $projectRoot ".env.alpaca.local"

function Import-SimpleEnvFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if (-not (Test-Path $Path)) {
        return
    }

    foreach ($rawLine in Get-Content -Path $Path -Encoding UTF8) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
            continue
        }
        $parts = $line.Split("=", 2)
        $name = $parts[0].Trim()
        $value = $parts[1].Trim()
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

Import-SimpleEnvFile -Path $envFile

if (-not $env:ALPACA_API_KEY -and $env:APCA_API_KEY_ID) {
    $env:ALPACA_API_KEY = $env:APCA_API_KEY_ID
}
if (-not $env:ALPACA_SECRET_KEY -and $env:APCA_API_SECRET_KEY) {
    $env:ALPACA_SECRET_KEY = $env:APCA_API_SECRET_KEY
}
if (-not $env:ALPACA_PAPER_TRADE) {
    $env:ALPACA_PAPER_TRADE = "true"
}

if (-not $env:ALPACA_API_KEY -or -not $env:ALPACA_SECRET_KEY) {
    Write-Error "Faltan credenciales Alpaca. Completa .env.alpaca.local con APCA_API_KEY_ID y APCA_API_SECRET_KEY o exporta ALPACA_API_KEY y ALPACA_SECRET_KEY."
    exit 1
}

$uvx = (Get-Command uvx -ErrorAction SilentlyContinue)
if (-not $uvx) {
    Write-Error "uvx no esta disponible en PATH. Reinicia Codex o instala uv antes de usar el MCP oficial de Alpaca."
    exit 1
}

& $uvx.Source "alpaca-mcp-server" @args
exit $LASTEXITCODE
