@echo off
REM One-command local launch for Windows: sets up the venv on first run,
REM starts the orchestrator + N local helpers, and opens the dashboard.
REM Usage:  run.bat            (main + 2 local helpers)
REM         run.bat 0          (main only)
REM         run.bat 4          (main + 4 local helpers)
REM Close the opened windows to stop each process.
REM
REM NOT independently tested on a real Windows machine — if it doesn't
REM behave as described, run the manual steps in README.md instead and
REM let the project maintainer know what broke.
setlocal enabledelayedexpansion
cd /d "%~dp0"

set HELPERS=%1
if "%HELPERS%"=="" set HELPERS=2
if not defined OVERCLOCK_MAIN_PORT (set MAIN_PORT=5050) else (set MAIN_PORT=%OVERCLOCK_MAIN_PORT%)
if not defined OVERCLOCK_HELPER_BASE_PORT (set BASE_PORT=5001) else (set BASE_PORT=%OVERCLOCK_HELPER_BASE_PORT%)

if not exist .venv (
  echo Setting up virtual environment ^(first run only^)...
  python -m venv .venv
  .venv\Scripts\pip install -q -r requirements.txt
)

set /a i=0
:helper_loop
if !i! GEQ %HELPERS% goto helpers_done
set /a port=%BASE_PORT%+!i!
start "Overclock Helper !i! (port !port!)" .venv\Scripts\python app_helper.py --port !port!
set /a i+=1
goto helper_loop
:helpers_done

start "Overclock Dashboard" .venv\Scripts\python app_main.py --port %MAIN_PORT%

timeout /t 3 /nobreak >nul
start "" "http://localhost:%MAIN_PORT%"

echo Dashboard: http://localhost:%MAIN_PORT%
echo Close the opened "Overclock ..." windows to stop each process.
