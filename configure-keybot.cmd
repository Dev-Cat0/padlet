@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv-keybot\Scripts\python.exe" (
  call start-keybot.cmd
  exit /b
)
".venv-keybot\Scripts\python.exe" -m keybot.setup
pause
