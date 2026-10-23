@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv-keybot\Scripts\python.exe" (
  echo start-keybot.cmd first.
  pause
  exit /b 1
)
".venv-keybot\Scripts\python.exe" -m keybot.control status
pause
