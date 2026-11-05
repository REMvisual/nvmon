@echo off
setlocal enabledelayedexpansion

REM =========================================================================
REM  Run the monitor: controller (:9997) + unified dashboard.
REM
REM  What happens on a double-click (v0.9.2):
REM    1. This window hands off to a MINIMIZED classic console and closes, so
REM       the monitor never sits on top of the dashboard / the show. Open it
REM       from the taskbar ("nvmon - monitor") to see the live log.
REM    2. The minimized console prints a status banner (version, URLs,
REM       startup state) and runs the controller in the foreground.
REM       Closing that window kills the controller. kill_monitor.bat is the
REM       safety-net stop.
REM    3. A shortcut to this script is kept in the current user's Startup
REM       folder, so the monitor comes back at every logon of this user.
REM       The check runs on every launch and is silent when already present.
REM
REM  No UAC: the controller does not require admin (it only HTTP-fans-out to
REM  agents, reads files in secrets\, and binds an unprivileged port). It
REM  runs as the calling user. The Startup shortcut is per-user too, so it
REM  never prompts (the PUBLIC Startup folder would need admin).
REM
REM  Self-bootstrapping: if .\python\python.exe is missing this script fires
REM  bats\install.bat ONCE via UAC to do the admin-only setup (firewall,
REM  etc.), then runs the controller non-elevated.
REM
REM  Usage:
REM     run_monitor.bat                  Start controller, open browser.
REM     run_monitor.bat --no-browser     Don't auto-open the browser.
REM     run_monitor.bat --no-minimize    Keep this window in front.
REM     run_monitor.bat --no-startup     Don't add the Startup shortcut.
REM     run_monitor.bat --remove-startup Remove the Startup shortcut and exit.
REM     run_monitor.bat --help
REM =========================================================================

set "OPEN_BROWSER=1"
set "MINIMIZE=1"
set "ADD_STARTUP=1"
set "REMOVE_STARTUP="
set "PASSTHRU="
:argloop
if "%~1"=="" goto argdone
if /I "%~1"=="--help"           goto :show_help
if /I "%~1"=="-h"               goto :show_help
if /I "%~1"=="/?"               goto :show_help
if /I "%~1"=="--no-browser"     set "OPEN_BROWSER=" & set "PASSTHRU=!PASSTHRU! --no-browser"
if /I "%~1"=="--no-minimize"    set "MINIMIZE="
if /I "%~1"=="--no-startup"     set "ADD_STARTUP=" & set "PASSTHRU=!PASSTHRU! --no-startup"
if /I "%~1"=="--remove-startup" set "REMOVE_STARTUP=1" & set "MINIMIZE="
shift
goto argloop
:argdone

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "PY_EXE=%SCRIPT_DIR%\python\python.exe"
set "INSTALL_BAT=%SCRIPT_DIR%\bats\install.bat"
set "SHORTCUT=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\nvmon monitor.lnk"

if defined REMOVE_STARTUP goto :remove_startup

REM ----- Hand off to a minimized console ------------------------------------
REM Only the first instance does this (NVMON_MONITOR_CHILD marks the child).
REM conhost.exe is asked for explicitly so the minimized state is honoured
REM even when the default terminal is Windows Terminal, which ignores
REM `start /min` for its own tabs.
if defined MINIMIZE if not "%NVMON_MONITOR_CHILD%"=="1" (
    set "NVMON_MONITOR_CHILD=1"
    start "nvmon - monitor" /min conhost.exe cmd /c ""%~f0" --no-minimize!PASSTHRU!"
    exit /b 0
)
REM Title prefix "nvmon - monitor" is what kill_monitor.bat sweeps for.
title nvmon - monitor

REM ----- Self-bootstrap if Python isn't here yet ----------------------------
REM This is the ONLY UAC the operator ever sees on the monitor side, and
REM only on a fresh copy of NVMON\. Run install.bat elevated, wait for it,
REM then continue non-elevated.
if not exist "%PY_EXE%" (
    if not exist "%INSTALL_BAT%" (
        echo [monitor] FATAL: bats\install.bat not found next to run_monitor.bat.
        echo           Re-copy NVMON and try again.
        pause
        exit /b 4
    )
    echo.
    echo  ===========================================================
    echo   nvmon monitor: first run on this box -- bootstrapping.
    echo   Accept the UAC prompt to install Python + firewall rules.
    echo   This happens once; future launches are silent and non-admin.
    echo  ===========================================================
    echo.
    powershell -NoProfile -Command ^
        "try { Start-Process -FilePath '%INSTALL_BAT%' -ArgumentList '--monitor','--quiet' -Verb RunAs -WorkingDirectory '%SCRIPT_DIR%' -Wait -ErrorAction Stop } " ^
        "catch { Write-Host ('elevation failed: ' + $_.Exception.Message) -ForegroundColor Red; exit 1 }"
    if errorlevel 1 (
        echo.
        echo [monitor] elevation declined. Right-click run_monitor.bat -^> "Run as administrator"
        echo           ONCE to bootstrap, or run bats\install.bat manually.
        echo.
        pause
        exit /b 1
    )
    if not exist "%PY_EXE%" (
        echo [monitor] ERROR: bats\install.bat did not produce %PY_EXE%
        echo [monitor]        Check logs\install.log
        pause
        exit /b 2
    )
)

REM ----- Startup shortcut (per-user, no UAC) ---------------------------------
set "STARTUP_STATE=not registered  (run_monitor.bat adds it; --no-startup skips)"
if defined ADD_STARTUP (
    if exist "%SHORTCUT%" (
        set "STARTUP_STATE=registered  (%%APPDATA%%\...\Startup\nvmon monitor.lnk)"
    ) else (
        powershell -NoProfile -Command ^
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('%SHORTCUT%');" ^
            "$s.TargetPath = '%~f0';" ^
            "$s.WorkingDirectory = '%SCRIPT_DIR%';" ^
            "$s.Description = 'nvmon monitor (controller + dashboard)';" ^
            "$s.WindowStyle = 7;" ^
            "$s.Save()" >nul 2>&1
        if exist "%SHORTCUT%" (
            set "STARTUP_STATE=registered now  (starts at every logon of %USERNAME%)"
        ) else (
            set "STARTUP_STATE=could not create shortcut  (see run_monitor.bat --help)"
        )
    )
) else (
    if exist "%SHORTCUT%" set "STARTUP_STATE=registered  (--no-startup only skips the check)"
)

REM ----- Facts for the banner -----------------------------------------------
set "VER=?"
REM Read `__version__ = "x.y.z"` straight from the package file. (A python -c
REM one-liner here fights cmd's quote/parenthesis parsing inside for /f.)
for /f "tokens=2 delims== " %%V in ('findstr /B /C:"__version__ = " "%SCRIPT_DIR%\nvmon\__init__.py"') do set "VER=%%~V"
set "LAN_IP="
for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | Sort-Object -Property { if ($_.IPAddress -like '10.1.110.*') { 0 } else { 1 } } | Select-Object -First 1 -ExpandProperty IPAddress)" 2^>nul`) do set "LAN_IP=%%I"
if not defined LAN_IP set "LAN_IP=%COMPUTERNAME%"
set "PORT_STATE=free"
netstat -ano -p TCP | findstr /R /C:":9997 .*LISTENING" >nul 2>&1
if %errorlevel% equ 0 set "PORT_STATE=IN USE -- a controller may already be running (kill_monitor.bat for a clean start)"
set "TODAY="
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"`) do set "TODAY=%%D"

REM ----- Banner --------------------------------------------------------------
color 0F
title nvmon - monitor v%VER% - %COMPUTERNAME%
cls
echo.
echo   ================================================================
echo.
echo     nvmon monitor                                   v%VER%
echo     %COMPUTERNAME%
echo.
echo   ----------------------------------------------------------------
echo.
echo     dashboard   http://localhost:9997
echo                 http://%LAN_IP%:9997      (other machines)
echo     log file    logs\nvmon-%TODAY%.log
echo     port 9997   %PORT_STATE%
echo     startup     %STARTUP_STATE%
echo.
echo     stop        close this window, Ctrl-C, or kill_monitor.bat
echo     this window is minimized on launch -- find it in the taskbar
echo.
echo   ================================================================
echo.
if not "%PORT_STATE%"=="free" timeout /t 3 >nul

REM ----- Run controller in foreground ---------------------------------------
if defined OPEN_BROWSER (
    "%PY_EXE%" -m nvmon controller --open-browser
) else (
    "%PY_EXE%" -m nvmon controller
)
set "RC=%errorlevel%"

REM Falls through here when the controller exits.
REM Only pause-for-keypress when the controller crashed (rc != 0). On a clean
REM exit (rc == 0) we close immediately so re-launching from a script doesn't
REM sit waiting for Enter.
echo.
echo [monitor] controller exited (rc=%RC%).
if not "%RC%"=="0" (
    echo [monitor] Crashed at startup. Scroll up for the error, or check
    echo           logs\nvmon-^<today^>.log.
    echo.
    echo Press any key to close this window.
    pause >nul
)
exit /b %RC%


:remove_startup
if exist "%SHORTCUT%" (
    del /q "%SHORTCUT%" >nul 2>&1
    if exist "%SHORTCUT%" (
        echo [monitor] could not remove "%SHORTCUT%"
        exit /b 3
    )
    echo [monitor] removed Startup shortcut: "%SHORTCUT%"
) else (
    echo [monitor] no Startup shortcut present ^("%SHORTCUT%"^)
)
echo           The monitor will no longer start at logon. Run run_monitor.bat
echo           once ^(without --no-startup^) to put it back.
exit /b 0


:show_help
echo.
echo nvmon run_monitor.bat
echo.
echo Usage:
echo   run_monitor.bat                   Start controller minimized, open browser,
echo                                     make sure the Startup shortcut exists.
echo   run_monitor.bat --no-browser      Don't auto-open the browser.
echo   run_monitor.bat --no-minimize     Keep the console in front.
echo   run_monitor.bat --no-startup      Don't add the per-user Startup shortcut.
echo   run_monitor.bat --remove-startup  Remove the Startup shortcut and exit.
echo   run_monitor.bat --help            Show this help.
echo.
echo Startup shortcut: %%APPDATA%%\Microsoft\Windows\Start Menu\Programs\Startup\nvmon monitor.lnk
echo (current user only, no UAC). Self-bootstraps via bats\install.bat (one UAC
echo prompt) the very first time. After that the controller runs as your normal
echo user -- no UAC, ever. Closing the console kills the controller.
echo.
exit /b 0
