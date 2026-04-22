@echo off
setlocal
call "%~dp0trading-bot-venv.cmd" live-check --venue mt5
call "%~dp0trading-bot-venv.cmd" preview-order --source mt5 --validate-live
call "%~dp0trading-bot-venv.cmd" run-once --source mt5 --live
endlocal
