@echo off
rem Keeps the recorder running: restarts it 10 seconds after any exit.
rem Exit code 3 means another recorder is already running, so this copy stops.
cd /d "%~dp0..\.."
if "%POLYDATA_DIR%"=="" set POLYDATA_DIR=C:\polydata
if not exist "%POLYDATA_DIR%\logs" mkdir "%POLYDATA_DIR%\logs"
:loop
"%POLYDATA_DIR%\venv\Scripts\python.exe" -m recorder >> "%POLYDATA_DIR%\logs\console.log" 2>&1
if %errorlevel%==3 exit /b 3
ping -n 11 127.0.0.1 >nul
goto loop
