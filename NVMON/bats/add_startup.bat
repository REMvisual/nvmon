@echo off
setlocal
title nvmon - add to startup

REM The opposite of remove_startup.bat: puts the nvmon listener back on
REM Windows startup. It clears the .no_autostart sentinel and re-registers
REM the "nvmon Listener" Scheduled Task (ONLOGON, highest privileges, owned
REM by the logged-in user) by calling install.bat --listener --quiet, which
REM is the canonical registration path. Then it starts the task and prints
REM the task status so you can see it is armed.
REM
REM Safe to run anytime. Idempotent. Self-elevates (one UAC prompt).

set "TASK_NAME=nvmon Listener"
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
REM This script lives in bats\ — the product root is one level up.
for %%I in ("%SCRIPT_DIR%\..") do set "ROOT=%%~fI"
set "INSTALL_BAT=%SCRIPT_DIR%\install.bat"
set "SENTINEL=%ROOT%\.no_autostart"

echo.
echo Adding nvmon listener to startup...
echo.

REM ----- elevate once -------------------------------------------------------
fltmc >nul 2>&1
if not %errorlevel% equ 0 (
    echo   not elevated -- requesting Administrator ^(one UAC prompt^)...
    powershell -NoProfile -Command ^
        "try { Start-Process -FilePath '%~f0' -Verb RunAs -WorkingDirectory '%SCRIPT_DIR%' -ErrorAction Stop } " ^
        "catch { Write-Host ('elevation declined: ' + $_.Exception.Message) -ForegroundColor Red; exit 1 }"
    if errorlevel 1 (
        echo   ERROR: elevation declined. Right-click add_startup.bat -^> Run as administrator.
        pause
        exit /b 1
    )
    exit /b 0
)

if not exist "%INSTALL_BAT%" (
    echo   ERROR: %INSTALL_BAT% not found.
    pause
    exit /b 2
)

REM 1. Sentinel left by remove_startup.bat — install.bat clears it too, but
REM    say so explicitly here because it is the thing people forget.
if exist "%SENTINEL%" (
    del /q "%SENTINEL%" >nul 2>&1
    echo   [removed] .no_autostart sentinel
) else (
    echo   [skip]    no .no_autostart sentinel
)

REM 2. Register the Scheduled Task (install.bat deletes any stale entry first).
echo   [run]     install.bat --listener --quiet
call "%INSTALL_BAT%" --listener --quiet
if errorlevel 1 (
    echo   [FAIL]    install.bat returned %errorlevel% -- see logs\install-*.log
    pause
    exit /b 3
)

REM 3. Start it now and show what Windows has on file.
schtasks /Query /TN "%TASK_NAME%" >nul 2>&1
if errorlevel 1 (
    echo   [FAIL]    Scheduled Task "%TASK_NAME%" still not present
    pause
    exit /b 4
)
schtasks /Query /TN "%TASK_NAME%" /FO LIST /V | findstr /C:"TaskName" /C:"Status" /C:"Schedule Type" /C:"Run As User" /C:"Logon Mode" /C:"Task To Run" /C:"Last Run Time" /C:"Last Result"
schtasks /Run /TN "%TASK_NAME%" >nul 2>&1 && echo   [started] listener task running now

echo.
echo Done. nvmon listener will auto-start at every logon of %USERDOMAIN%\%USERNAME%.
echo Verify any time with:   schtasks /Query /TN "%TASK_NAME%" /V /FO LIST
echo.
pause
exit /b 0
