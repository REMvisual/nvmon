@echo off
setlocal enabledelayedexpansion
title nvmon - redeploy
REM =========================================================================
REM  Redeploy helper for nvmon. Run this on a render node OR the monitor
REM  host AFTER you've copied a fresh NVVMON tree over the old one. It:
REM
REM   1. Stops any running listener/monitor (frees ports 9996/9997/9998).
REM   2. Wipes __pycache__ so stale .pyc never wins over your new .py.
REM   3. Prints the version that's now on disk so you can confirm the
REM      copy succeeded (you should see v0.2.0 or newer).
REM   4. Restarts the right side based on the optional argument:
REM        redeploy.bat listener   (default — agent + launcher)
REM        redeploy.bat monitor    (controller + dashboard)
REM        redeploy.bat verify     (no restart, just print version)
REM
REM  If the dashboard or buttons "still don't work" after a code change,
REM  this script is the first thing to run.  Stale processes and stale
REM  __pycache__ are by far the most common cause.
REM =========================================================================

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
REM This script lives in bats\ — redirect SCRIPT_DIR to the NVMON product root.
for %%I in ("%SCRIPT_DIR%\..") do set "SCRIPT_DIR=%%~fI"
set "PY_EXE=%SCRIPT_DIR%\python\python.exe"
set "MODE=%~1"
if "%MODE%"=="" set "MODE=listener"

REM ----- Auto-elevate -------------------------------------------------------
REM Same pattern as run_listener.bat / run_monitor.bat. taskkill needs admin
REM and so does the eventual call into run_*.bat anyway.
fltmc >nul 2>&1
if %errorlevel% neq 0 (
    echo [redeploy] not elevated — accept UAC to relaunch as Administrator.
    powershell -NoProfile -Command ^
        "try { Start-Process -FilePath '%~f0' -Verb RunAs -WorkingDirectory '%~dp0' -ArgumentList '%MODE%' -ErrorAction Stop } " ^
        "catch { Write-Host ('elevation failed: ' + $_.Exception.Message) -ForegroundColor Red; exit 1 }"
    if errorlevel 1 pause
    exit /b 0
)

echo.
echo ============================================================
echo   nvmon redeploy ^(%MODE%^)
echo ============================================================

REM ----- Step 1: stop anything currently bound to our ports -----------------
echo [1/4] Stopping running services...
for %%P in (9996 9997 9998) do (
    for /f "tokens=5" %%I in ('netstat -ano ^| findstr ":%%P " ^| findstr LISTENING') do (
        echo       killing PID %%I on TCP %%P
        taskkill /F /PID %%I >nul 2>&1
    )
)

REM ----- Step 2: wipe __pycache__ ------------------------------------------
echo [2/4] Removing stale __pycache__ directories...
for /d /r "%SCRIPT_DIR%\nvmon" %%D in (__pycache__) do (
    if exist "%%D" rmdir /s /q "%%D" >nul 2>&1
)

REM ----- Step 3: print version ----------------------------------------------
echo [3/4] Version on disk:
"%PY_EXE%" -c "from nvmon import __version__; print('       v' + __version__)"
if errorlevel 1 (
    echo       ERROR: could not import nvmon. Is python\python.exe present?
    pause
    exit /b 2
)

if /I "%MODE%"=="verify" goto :end

REM ----- Step 4: restart ----------------------------------------------------
echo [4/4] Restarting %MODE%...
if /I "%MODE%"=="listener" (
    if exist "%SCRIPT_DIR%\run_listener.bat" (
        call "%SCRIPT_DIR%\run_listener.bat"
    ) else (
        echo       run_listener.bat not found — start manually: python\python.exe -m nvmon agent
    )
    goto :end
)
if /I "%MODE%"=="monitor" (
    if exist "%SCRIPT_DIR%\run_monitor.bat" (
        call "%SCRIPT_DIR%\run_monitor.bat"
    ) else (
        echo       run_monitor.bat not found — start manually: python\python.exe -m nvmon controller
    )
    goto :end
)

echo Unknown mode: %MODE%
echo Usage: redeploy.bat [listener^|monitor^|verify]
exit /b 1

:end
echo.
echo Done.
endlocal
