@echo off
rem The ETH recorder: a second, separate copy of the recorder (see recorder\config.py, POLY_ASSET).
rem Its own data folder and lock; it shares only the Python environment of the BTC recorder.
rem Exit code 3 means another ETH recorder is already running, so this copy stops.
cd /d "%~dp0..\.."
set POLY_ASSET=eth
set POLYDATA_DIR=C:\polydata_eth
if not exist "%POLYDATA_DIR%\logs" mkdir "%POLYDATA_DIR%\logs"
:loop
"C:\polydata\venv\Scripts\python.exe" -m recorder >> "%POLYDATA_DIR%\logs\console.log" 2>&1
if %errorlevel%==3 exit /b 3
ping -n 11 127.0.0.1 >nul
goto loop
