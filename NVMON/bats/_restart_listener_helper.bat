@echo off
setlocal enabledelayedexpansion

REM ============================================================================
REM  _restart_listener_helper.bat
REM
REM  Spawned (detached) by nvmon/agent/system.py::restart_listener().
REM  Bounces the running listener:
REM     1. Wait <delay> seconds so the agent's HTTP 202 can flush.
REM     2. If the 'nvmon Listener' Scheduled Task is registered, schtasks /End
REM        + schtasks /Run. UAC-free, fastest path.
REM     3. Otherwise fall back to kill_listener.bat + run_listener.bat.
REM  Writes everything to logs/restart_listener-<stamp>.log so failures are
REM  diagnosable instead of silent.
REM
REM  Usage:
REM     _restart_listener_helper.bat [delay_seconds]
REM     defaults: delay=2
REM ============================================================================

set "DELAY=%~1"
if "%DELAY%"=="" set "DELAY=2"

set "BASE=%~dp0"
if "%BASE:~-1%"=="\" set "BASE=%BASE:~0,-1%"
REM This helper lives in bats\ — redirect BASE to the NVMON product root
REM so kill_listener.bat / run_listener.bat siblings resolve correctly.
for %%I in ("%BASE%\..") do set "BASE=%%~fI"

REM Build a YYYYMMDD-HHMMSS stamp without depending on locale.
for /f "tokens=2 delims==" %%a in ('wmic os get LocalDateTime /value 2^>nul') do set "RAW=%%a"
if defined RAW (
    set "STAMP=!RAW:~0,8!-!RAW:~8,6!"
) else (
    set "STAMP=fallback"
)

set "LOG=%BASE%\logs\restart_listener-%STAMP%.log"
set "TASK=nvmon Listener"

if not exist "%BASE%\logs" mkdir "%BASE%\logs" 2>nul

>  "%LOG%" echo --- nvmon listener restart helper ---
>> "%LOG%" echo started at %date% %time%
>> "%LOG%" echo base   : %BASE%
>> "%LOG%" echo delay  : %DELAY%s
>> "%LOG%" echo task   : %TASK%

REM Step 1: wait so the HTTP response can flush before we kill anything.
timeout /t %DELAY% /nobreak >nul

REM Step 2: probe Task Scheduler. errorlevel 0 = task registered; non-zero = absent.
schtasks /Query /TN "%TASK%" >nul 2>&1
if not errorlevel 1 (
    >> "%LOG%" echo path   : Scheduled Task End/Run
    >> "%LOG%" echo running: schtasks /End /TN "%TASK%"
    schtasks /End /TN "%TASK%" >> "%LOG%" 2>&1
    timeout /t 2 /nobreak >nul
    >> "%LOG%" echo running: schtasks /Run /TN "%TASK%"
    schtasks /Run /TN "%TASK%" >> "%LOG%" 2>&1
    goto :done
)

>> "%LOG%" echo path   : kill_listener.bat + run_listener.bat (no scheduled task)

if exist "%BASE%\kill_listener.bat" (
    >> "%LOG%" echo running: kill_listener.bat
    call "%BASE%\kill_listener.bat" >> "%LOG%" 2>&1
) else (
    >> "%LOG%" echo WARN: kill_listener.bat not found at %BASE%
    REM Best-effort: kill whatever is on 9998 + 9996 directly.
    for /f "tokens=5" %%p in ('netstat -ano -p TCP ^| findstr /R /C:":9998 .*LISTENING"') do (
        >> "%LOG%" echo running: taskkill /F /T /PID %%p
        taskkill /F /T /PID %%p >> "%LOG%" 2>&1
    )
    for /f "tokens=5" %%p in ('netstat -ano -p TCP ^| findstr /R /C:":9996 .*LISTENING"') do (
        >> "%LOG%" echo running: taskkill /F /T /PID %%p
        taskkill /F /T /PID %%p >> "%LOG%" 2>&1
    )
)

timeout /t 2 /nobreak >nul

if exist "%BASE%\run_listener.bat" (
    >> "%LOG%" echo running: start "" /min run_listener.bat
    start "" /min "%BASE%\run_listener.bat" >> "%LOG%" 2>&1
) else (
    >> "%LOG%" echo FATAL: run_listener.bat not found at %BASE% — cannot relaunch
)

:done
>> "%LOG%" echo finished at %date% %time%
exit /b 0
