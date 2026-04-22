$ErrorActionPreference = "Stop"

$tool = Join-Path (Split-Path -Parent $PSScriptRoot) ".venv\Scripts\trading-bot.exe"
if (-not (Test-Path $tool)) {
    Write-Error "No encuentro trading-bot.exe dentro del .venv. Reinstala el entorno estable del proyecto."
    exit 1
}

& $tool @args
exit $LASTEXITCODE
