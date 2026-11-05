@echo off
setlocal enabledelayedexpansion
title nvmon - remove from startup

REM Removes nvmon listener/monitor from Windows startup.
REM First-run of run_listener.bat / run_monitor.bat self-bootstraps install.bat,
REM which registers a Scheduled Task and a Public Desktop shortcut. This bat
REM removes them so the machine boots clean.
REM
REM Safe to run anytime. Idempotent — missing items are silently skipped.
REM Must be Run As Administrator.

set "TASK_NAME=nvmon Listener"
set "LEGACY_STARTUP=%ProgramData%\Microsoft\Windows\Start Menu\Programs\StartUp\nvmon Listener.lnk"
set "MONITOR_SHORTCUT=%PUBLIC%\Desktop\nvmon Monitor.lnk"
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
REM This script lives in bats\ — redirect SCRIPT_DIR to the NVMON product root.
for %%I in ("%SCRIPT_DIR%\..") do set "SCRIPT_DIR=%%~fI"
set "SENTINEL=%SCRIPT_DIR%\.no_autostart"

echo.
echo Removing nvmon from startup...
echo.

net session >nul 2>&1
if errorlevel 1 (
    echo ERROR: must Run As Administrator.
    pause
    exit /b 1
)

REM Stop anything running first.
schtasks /End /TN "%TASK_NAME%" >nul 2>&1
taskkill /F /T /FI "WINDOWTITLE eq nvmon - listener*" >nul 2>&1
taskkill /F /T /FI "WINDOWTITLE eq nvmon - monitor*"  >nul 2>&1

REM 1. Scheduled Task (the actual startup hook on the listener side).
schtasks /Query /TN "%TASK_NAME%" >nul 2>&1
if errorlevel 1 (
    echo   [skip]    Scheduled Task "%TASK_NAME%" not present
) else (
    schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>&1
    schtasks /Query /TN "%TASK_NAME%" >nul 2>&1
    if errorlevel 1 (
        echo   [removed] Scheduled Task "%TASK_NAME%"
    ) else (
        echo   [FAIL]    could not delete Scheduled Task
    )
)

REM 2. Legacy Startup-folder shortcut (older installs).
if exist "%LEGACY_STARTUP%" (
    del /q "%LEGACY_STARTUP%" >nul 2>&1
    if exist "%LEGACY_STARTUP%" (
        echo   [FAIL]    could not delete legacy Startup-folder shortcut
    ) else (
        echo   [removed] legacy Startup-folder shortcut
    )
) else (
    echo   [skip]    no legacy Startup-folder shortcut
)

REM 3. Public Desktop "nvmon Monitor" shortcut.
if exist "%MONITOR_SHORTCUT%" (
    del /q "%MONITOR_SHORTCUT%" >nul 2>&1
    if exist "%MONITOR_SHORTCUT%" (
        echo   [FAIL]    could not delete Public Desktop "nvmon Monitor" shortcut
    ) else (
        echo   [removed] Public Desktop "nvmon Monitor" shortcut
    )
) else (
    echo   [skip]    no Public Desktop "nvmon Monitor" shortcut
)

REM 4. Drop a sentinel so run_listener.bat does NOT auto-recreate the
REM    Scheduled Task on the next launch. Without this, the very next
REM    double-click of run_listener.bat would see "no task" and re-bootstrap.
REM    To re-enable auto-start, delete .no_autostart or rerun install.bat --listener.
echo nvmon: auto-start at logon disabled by remove_startup.bat on %DATE% %TIME% > "%SENTINEL%"
if exist "%SENTINEL%" (
    echo   [wrote]   .no_autostart sentinel  ^(blocks run_listener.bat re-register^)
) else (
    echo   [FAIL]    could not write %SENTINEL%
)

echo.
echo Done. nvmon will not auto-start anymore.
echo Manual launches via run_listener.bat / run_monitor.bat still work and
echo will NOT re-add startup entries while .no_autostart is present.
echo.
echo To re-enable auto-start: rerun install.bat --listener  (or delete .no_autostart).
echo.
pause
exit /b 0
