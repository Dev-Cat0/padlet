@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\ready" goto ready
if exist ".venv\Scripts\python.exe" goto install
py -3 -m venv .venv
if errorlevel 1 python -m venv .venv
if not exist ".venv\Scripts\python.exe" goto failed
:install
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m playwright install chromium
if errorlevel 1 goto failed
echo installed>.venv\ready
:ready
.venv\Scripts\python.exe helper.py
if errorlevel 1 goto failed
exit /b
:failed
echo Install Python 3.12 or newer, then run start.cmd again.
pause
