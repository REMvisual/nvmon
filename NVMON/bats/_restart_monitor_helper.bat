@echo off
setlocal enabledelayedexpansion

REM ============================================================================
REM  _restart_monitor_helper.bat
REM
REM  Spawned (detached) by nvmon/controller/selfrestart.py::restart_monitor()
REM  -- the "Restart monitor" item in the dashboard's REM power menu.
REM  Bounces the controller running on this monitor box:
REM     stage 1  re-spawn this script minimized and exit at once, so the
REM              worker is no longer a descendant of the controller's python
REM              (kill_monitor.bat uses taskkill /T, which would take a child
REM              helper down with the controller).
REM     stage 2  1. wait <delay> seconds so the HTTP 202 can flush
REM              2. kill_monitor.bat  (closes the "nvmon - monitor" console +
REM                 python child, sweeps TCP 9997 for orphans)
REM              3. run_monitor.bat   (comes back minimized, re-checks the
REM                 per-user Startup shortcut; --no-browser unless asked)
REM  Everything goes to logs\restart_monitor-<stamp>.log.
REM
REM  Usage:
REM     _restart_monitor_helper.bat [delay_seconds] [browser|nobrowser]
REM     defaults: delay=2, nobrowser
REM ============================================================================

set "DELAY=%~1"
if "%DELAY%"=="" set "DELAY=2"
set "BROWSER=%~2"
if "%BROWSER%"=="" set "BROWSER=nobrowser"

set "BASE=%~dp0"
if "%BASE:~-1%"=="\" set "BASE=%BASE:~0,-1%"
REM This helper lives in bats\ -- redirect BASE to the NVMON product root
REM so kill_monitor.bat / run_monitor.bat siblings resolve correctly.
for %%I in ("%BASE%\..") do set "BASE=%%~fI"

REM ----- stage 1: detach from the controller's process tree -----------------
if not "%~3"=="--stage2" (
    start "" /min cmd /c ""%~f0" %DELAY% %BROWSER% --stage2"
    exit /b 0
)

REM ----- stage 2 ------------------------------------------------------------
REM Stamp via PowerShell (wmic is gone on Windows 11 24H2+).
set "STAMP="
for /f "usebackq delims=" %%a in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"`) do set "STAMP=%%a"
if not defined STAMP set "STAMP=fallback"

if not exist "%BASE%\logs" mkdir "%BASE%\logs" 2>nul
set "LOG=%BASE%\logs\restart_monitor-%STAMP%.log"

>  "%LOG%" echo --- nvmon monitor restart helper ---
>> "%LOG%" echo started at %date% %time%
>> "%LOG%" echo base    : %BASE%
>> "%LOG%" echo delay   : %DELAY%s
>> "%LOG%" echo browser : %BROWSER%

REM Step 1: let the controller's HTTP response flush before killing it.
timeout /t %DELAY% /nobreak >nul

REM Step 2: stop the controller (and its console).
if exist "%BASE%\kill_monitor.bat" (
    >> "%LOG%" echo running : kill_monitor.bat
    call "%BASE%\kill_monitor.bat" >> "%LOG%" 2>&1
) else (
    >> "%LOG%" echo WARN    : kill_monitor.bat not found -- killing TCP 9997 listeners directly
    for /f "tokens=5" %%p in ('netstat -ano -p TCP ^| findstr /R /C:":9997 .*LISTENING"') do (
        >> "%LOG%" echo running : taskkill /F /T /PID %%p
        taskkill /F /T /PID %%p >> "%LOG%" 2>&1
    )
)

REM Give the port a moment to be released, then confirm.
timeout /t 2 /nobreak >nul
netstat -ano -p TCP | findstr /R /C:":9997 .*LISTENING" >nul 2>&1
if %errorlevel% equ 0 (
    >> "%LOG%" echo WARN    : something still listens on 9997 after kill_monitor.bat -- relaunch may warn
) else (
    >> "%LOG%" echo ok      : port 9997 free
)

REM Step 3: relaunch. run_monitor.bat hands itself off to a minimized
REM console and re-checks the Startup shortcut; nothing else to do here.
if exist "%BASE%\run_monitor.bat" (
    REM Explicit `cmd /c`: `start x.bat` runs it under `cmd /K`, which would
    REM leave an idle minimized prompt behind once run_monitor.bat has handed
    REM off to its own console and returned.
    if /I "%BROWSER%"=="browser" (
        >> "%LOG%" echo running : run_monitor.bat
        start "" /min cmd /c ""%BASE%\run_monitor.bat""
    ) else (
        >> "%LOG%" echo running : run_monitor.bat --no-browser
        start "" /min cmd /c ""%BASE%\run_monitor.bat" --no-browser"
    )
) else (
    >> "%LOG%" echo FATAL   : run_monitor.bat not found at %BASE% -- controller NOT relaunched
)

REM Step 4: report whether it came back (up to ~20 s).
set /a TRIES=0
:waitup
timeout /t 2 /nobreak >nul
netstat -ano -p TCP | findstr /R /C:":9997 .*LISTENING" >nul 2>&1
if %errorlevel% equ 0 (
    >> "%LOG%" echo ok      : controller listening on 9997 again
    goto :done
)
set /a TRIES+=1
if %TRIES% lss 10 goto :waitup
>> "%LOG%" echo WARN    : controller not listening on 9997 after ~20 s -- check the "nvmon - monitor" window / logs\nvmon-^<today^>.log

:done
>> "%LOG%" echo finished at %date% %time%
exit /b 0
