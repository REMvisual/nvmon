@echo off
setlocal enabledelayedexpansion
title nvmon - listener

REM =========================================================================
REM  Run the listener: agent (:9998) + launcher (:9996) on this render node.
REM
REM  ONE console. Output from both daemons interleaves into this window.
REM  Closing the window or pressing Ctrl-C stops both children cleanly.
REM
REM  Self-bootstrapping: if the enclosed Python is missing, this script
REM  invokes install.bat --listener first. So a fresh node only needs the
REM  one shortcut.
REM
REM  Auto-elevation: configureQsync2.exe and configureDdisplay.exe both
REM  refuse to run without Administrator. If we boot non-elevated the
REM  agent reports UNLOCKED for QSync and empty displays — exactly the
REM  "false data" failure mode. So this script ALWAYS relaunches itself
REM  via PowerShell -Verb RunAs if we're not already admin.
REM
REM  Usage:
REM     run_listener.bat            Start agent + launcher.
REM     run_listener.bat --help
REM =========================================================================

if /I "%~1"=="--help" goto :show_help
if /I "%~1"=="-h"     goto :show_help
if /I "%~1"=="/?"     goto :show_help

REM ----- Auto-elevate -------------------------------------------------------
REM Order matters here:
REM   1. Check elevation FIRST. If we're already elevated (e.g. the
REM      Scheduled Task launched this cmd, or the user ran-as-admin
REM      manually), fall straight through to running the listener.
REM      Without this guard, an elevated cmd would re-fire schtasks /Run
REM      and infinite-loop the task against itself.
REM
REM   2. If we're NOT elevated, try the no-prompt path: /Run the
REM      pre-registered Scheduled Task. The task is owned by the current
REM      user with /RL HIGHEST, so a filtered (non-elevated) token from
REM      the same user can /Run it and Task Scheduler launches it with
REM      the user's full admin token — no UAC.
REM
REM   3. Only if no task is registered (or /Run fails) do we fall back
REM      to the PowerShell -Verb RunAs prompt.
REM
REM `fltmc` is the most reliable elevation probe — it doesn't depend on
REM the Server service the way `net session` does.

REM ----- Single-click flow -------------------------------------------------
REM Goal: the operator double-clicks run_listener.bat. They see UAC AT MOST
REM ONCE EVER (the very first time, to register the Scheduled Task). After
REM that, every click triggers the pre-registered task via the Task
REM Scheduler COM API, which is documented as not requiring UAC for tasks
REM the calling user owns.
REM
REM We auto-heal:
REM   * No task at all       -> auto-run install.bat --listener (UAC once).
REM   * Task owned by someone else (legacy group-owned install) -> ditto.
REM   * Task owned by us     -> Start-ScheduledTask, silent.
REM
REM There is no scenario in steady state where this script needs the
REM operator to manually run install.bat --listener -- it does that itself
REM exactly once.

set "TASK_NAME=nvmon Listener"
set "INSTALL_BAT=%~dp0bats\install.bat"
set "INSTALL_ARGS=--listener --quiet"

fltmc >nul 2>&1
if %errorlevel% equ 0 (
    echo [listener] running as Administrator -- good.
    goto :run_listener
)

REM ----- Manual-mode sentinel ----------------------------------------------
REM remove_startup.bat creates .no_autostart next to this script. While that
REM file is present, we run the listener WITHOUT touching the Scheduled Task
REM so the operator can do a one-off manual launch that doesn't re-add the
REM logon hook. Self-elevate via UAC (the listener still needs admin for the
REM NVIDIA CLI tools) and re-enter — the elevated instance trips the fltmc
REM branch above and runs the listener inline, never registering anything.
REM To re-enable auto-start: rerun install.bat --listener (it clears the file).
if exist "%~dp0.no_autostart" (
    echo [listener] .no_autostart present -- manual mode, NOT registering Scheduled Task.
    echo            ^(delete .no_autostart or rerun install.bat --listener to re-enable auto-start^)
    powershell -NoProfile -Command ^
        "try { Start-Process -FilePath '%~f0' -Verb RunAs -WorkingDirectory '%~dp0' -ErrorAction Stop } " ^
        "catch { Write-Host ('elevation declined: ' + $_.Exception.Message) -ForegroundColor Red; exit 1 }"
    if errorlevel 1 (
        echo [listener] elevation declined. Right-click run_listener.bat -^> Run as administrator,
        echo            or delete .no_autostart to restore the auto-elevate-via-task path.
        pause
        exit /b 1
    )
    exit /b 0
)

REM ----- Probe the task via PowerShell COM ---------------------------------
REM   exit 0 = task exists and is owned by this user (we can /Run silently)
REM   exit 2 = no task at all
REM   exit 3 = task exists but owned by a different principal
powershell -NoProfile -Command ^
    "$t = Get-ScheduledTask -TaskName '%TASK_NAME%' -ErrorAction SilentlyContinue;" ^
    "if (-not $t) { exit 2 };" ^
    "$prin = $t.Principal.UserId;" ^
    "$me   = $env:USERDOMAIN + '\\' + $env:USERNAME;" ^
    "if ($prin -notmatch [regex]::Escape($env:USERNAME)) {" ^
    "  Write-Host ('[listener] task principal: ' + $prin + ' (this user: ' + $me + ') -- ownership mismatch, will re-register.');" ^
    "  exit 3" ^
    "};" ^
    "try {" ^
    "  Start-ScheduledTask -TaskName '%TASK_NAME%' -ErrorAction Stop;" ^
    "  Write-Host '[listener] task started via COM API -- no UAC.';" ^
    "  exit 0" ^
    "} catch {" ^
    "  Write-Host ('[listener] Start-ScheduledTask failed: ' + $_.Exception.Message) -ForegroundColor Red;" ^
    "  exit 1" ^
    "}"
set "PSRC=%errorlevel%"

if %PSRC% equ 0 (
    echo            health:  http://%COMPUTERNAME%:9998/health
    echo            logs:    http://%COMPUTERNAME%:9998/logs?lines=200
    echo            stop:    schtasks /End /TN "%TASK_NAME%"
    timeout /t 3 >nul
    exit /b 0
)

REM No task, or wrong owner. Auto-run the installer. UAC fires ONCE here.
if %PSRC% equ 2 (
    echo [listener] First-time setup: registering the Scheduled Task.
    echo            ^(One UAC prompt now -- never again after this.^)
)
if %PSRC% equ 3 (
    echo [listener] Migrating Scheduled Task ownership to this user.
    echo            ^(One UAC prompt now -- caused by an older install.^)
)
if not exist "%INSTALL_BAT%" (
    echo [listener] FATAL: bats\install.bat not found next to run_listener.bat.
    echo            Re-copy NVVMON and try again.
    pause
    exit /b 4
)

powershell -NoProfile -Command ^
    "try { Start-Process -FilePath '%INSTALL_BAT%' -ArgumentList '--listener','--quiet' -Verb RunAs -WorkingDirectory '%~dp0' -Wait -ErrorAction Stop } " ^
    "catch { Write-Host ('elevation failed: ' + $_.Exception.Message) -ForegroundColor Red; exit 1 }"
if errorlevel 1 (
    echo.
    echo [listener] elevation declined. Right-click run_listener.bat -> Run as administrator
    echo            ONCE to bootstrap; after that double-clicking is silent.
    echo.
    pause
    exit /b 1
)

REM Installer creates and auto-starts the task. Verify.
powershell -NoProfile -Command ^
    "if (Get-ScheduledTask -TaskName '%TASK_NAME%' -ErrorAction SilentlyContinue) { Start-ScheduledTask -TaskName '%TASK_NAME%' -ErrorAction SilentlyContinue }"
echo [listener] setup complete. Listener is now running and will auto-start at every logon.
echo            Future double-clicks of run_listener.bat trigger silently.
timeout /t 4 >nul
exit /b 0

:run_listener

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "PY_EXE=%SCRIPT_DIR%\python\python.exe"
set "BIN_DIR=%SCRIPT_DIR%\bin"

REM ----- Self-bootstrap if Python isn't here yet ----------------------------
if not exist "%PY_EXE%" (
    echo [listener] enclosed Python not found — running bats\install.bat --listener
    call "%SCRIPT_DIR%\bats\install.bat" --listener --quiet
    if not exist "%PY_EXE%" (
        echo [listener] ERROR: bats\install.bat did not produce %PY_EXE%
        echo [listener]        Check logs\install.log
        pause
        exit /b 2
    )
)

REM ----- Capability sanity print --------------------------------------------
echo [listener] %COMPUTERNAME% — %PY_EXE%
call :probe configureMosaic.exe
call :probe configureDdisplay.exe
call :probe configureQsync2.exe

REM ----- Already running? ---------------------------------------------------
call :port_in_use 9998 && (
    echo [listener] WARNING: port 9998 already in use — agent may already be running.
    echo [listener]          Run kill_listener.bat first if you want a clean start.
)
call :port_in_use 9996 && (
    echo [listener] WARNING: port 9996 already in use — launcher may already be running.
)

REM ----- Run unified supervisor in this console ----------------------------
"%PY_EXE%" -m nvmon listener
set "RC=%errorlevel%"

echo.
echo [listener] exited (rc=%RC%).
if not "%RC%"=="0" (
    echo [listener] If the listener crashed at startup, scroll up to see why,
    echo            or check logs\nvmon-^<today^>.log.
)
echo.
echo Press any key to close this window.
pause >nul
exit /b %RC%


REM =========================================================================
REM  Subroutines
REM =========================================================================

:probe
set "EXE_NAME=%~1"
if exist "%BIN_DIR%\%EXE_NAME%" (
    echo [listener]   %EXE_NAME%: present
) else (
    echo [listener]   %EXE_NAME%: MISSING ^(subsystem will report 503^)
)
exit /b 0

:port_in_use
REM Returns errorlevel 0 if something is LISTENING on TCP %1.
set "PORT=%~1"
netstat -ano -p TCP | findstr /R /C:":%PORT% .*LISTENING" >nul 2>&1
exit /b %errorlevel%


:show_help
echo.
echo nvmon run_listener.bat
echo.
echo Usage:
echo   run_listener.bat        Start agent + launcher in this console.
echo   run_listener.bat --help Show this help.
echo.
echo Self-bootstraps: runs bats\install.bat --listener first if the enclosed
echo Python isn't present yet. Both daemons share this one window;
echo closing it (or Ctrl-C) stops both.
echo.
exit /b 0
