@echo off
title Smart Plug Server
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python is not installed or not on PATH. Install it from python.org.
  pause
  exit /b 1
)

if not exist config.json (
  echo config.json is missing. See HOW_TO_RUN.md.
  pause
  exit /b 1
)

python -c "import flask, tinytuya" >nul 2>nul
if errorlevel 1 (
  echo Installing required packages...
  python -m pip install -r requirements.txt
)

if not defined NOBROWSER start "" /min cmd /c "timeout /t 3 >nul & start http://localhost:8080"

echo Starting server. Keep this window open. Close it to stop the server.
python plug_server.py
echo.
echo Server stopped.
pause
