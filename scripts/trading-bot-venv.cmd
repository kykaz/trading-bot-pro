@echo off
setlocal
set "ROOT=%~dp0.."
set "PYTHON=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo No encuentro python.exe dentro del .venv estable del proyecto.
  exit /b 1
)
"%PYTHON%" -m trading_bot.main %*
exit /b %ERRORLEVEL%
