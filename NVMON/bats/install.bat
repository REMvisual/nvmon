@echo off
setlocal enabledelayedexpansion
title nvmon - install

REM =========================================================================
REM  Unified installer for nvmon — handles both deployment roles.
REM
REM  Usage:
REM     install.bat                 Install BOTH roles on this box (interactive).
REM     install.bat --listener      Render-node side only (agent + launcher).
REM     install.bat --monitor       Central-box side only (controller).
REM     install.bat --both          Same as default.
REM     install.bat --controller URL
REM                                 Listener: bake controller URL into nvmon.toml so
REM                                 the agent push-registers on boot. Example:
REM                                   install.bat --listener --controller http://192.0.2.10:9997
REM     install.bat --quiet         Skip prompts and "press any key" pauses.
REM     install.bat --help
REM
REM  Run as Administrator. Idempotent — rerun to repair, rotate password,
REM  or pick up new requirements.
REM
REM  Steps (always):
REM     1. Admin check.
REM     2. Bootstrap embedded Python 3.11 in .\python\ if missing.
REM     3. pip install -r requirements.txt into the enclosed Python.
REM     4. Firewall: UDP 5353 (mDNS).
REM
REM  Steps (--listener):
REM     5L. Check the NVIDIA CLI tools in bin\ (copied from %NVMON_NVIDIA_TOOLS% if set).
REM     6L. Firewall: TCP 9998 (agent), TCP 9996 (launcher).
REM     7L. Scheduled Task "nvmon Listener" (ONLOGON, RL HIGHEST, owned
REM         by current user) — boots run_listener.bat at logon with no UAC.
REM
REM  Steps (--monitor):
REM     5M. Prompt for / keep controller password in secrets\.
REM     6M. ACL secrets\ to current user + SYSTEM + Administrators.
REM     7M. Firewall: TCP 9997 (controller).
REM     8M. Public Desktop shortcut "nvmon Monitor" -> run_monitor.bat.
REM
REM  The controller does NOT require admin at runtime (no is_admin() gates),
REM  so run_monitor.bat starts non-elevated and never prompts UAC after this
REM  one-time install. The agent + launcher DO need admin, but the Scheduled
REM  Task gives them a no-UAC startup path.
REM =========================================================================

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
REM This script lives in bats\ — redirect SCRIPT_DIR to the NVMON product root
REM so every path below (python\, bin\, secrets\, logs\, run_*.bat) resolves
REM to the same place it did when this script lived in the root.
for %%I in ("%SCRIPT_DIR%\..") do set "SCRIPT_DIR=%%~fI"
set "LOG_DIR=%SCRIPT_DIR%\logs"
set "LOG_FILE=%LOG_DIR%\install.log"
set "PY_DIR=%SCRIPT_DIR%\python"
set "PY_EXE=%PY_DIR%\python.exe"
set "BIN_DIR=%SCRIPT_DIR%\bin"
set "SECRETS_DIR=%SCRIPT_DIR%\secrets"
set "PASSWORD_FILE=%SECRETS_DIR%\controller.password"
set "PY_VERSION=3.11.9"
set "PY_EMBED_URL=https://www.python.org/ftp/python/%PY_VERSION%/python-%PY_VERSION%-embed-amd64.zip"
set "GETPIP_URL=https://bootstrap.pypa.io/get-pip.py"
REM Optional: a folder that already holds configureMosaic/Ddisplay/Qsync2.exe
REM (e.g. an older tool's install). Set NVMON_NVIDIA_TOOLS before running to
REM have the installer copy them into bin\; unset = skip (bin\ ships them).
set "NVIDIA_SRC=%NVMON_NVIDIA_TOOLS%"

REM ----- Parse args ---------------------------------------------------------
set "QUIET="
set "DO_LISTENER="
set "DO_MONITOR="
set "ROLE_GIVEN="
set "CONTROLLER_URL="
:argloop
if "%~1"=="" goto argdone
if /I "%~1"=="--help"      goto :show_help
if /I "%~1"=="-h"          goto :show_help
if /I "%~1"=="/?"          goto :show_help
if /I "%~1"=="--quiet"     set "QUIET=1"
if /I "%~1"=="--listener"  ( set "DO_LISTENER=1" & set "ROLE_GIVEN=1" )
if /I "%~1"=="--monitor"   ( set "DO_MONITOR=1"  & set "ROLE_GIVEN=1" )
if /I "%~1"=="--both"      ( set "DO_LISTENER=1" & set "DO_MONITOR=1" & set "ROLE_GIVEN=1" )
if /I "%~1"=="--controller" (
    set "CONTROLLER_URL=%~2"
    shift
)
shift
goto argloop
:argdone

REM No role flag => both. Mirrors the old behaviour of "rerun whichever
REM installer matches your role" but lets a single-host setup do it in one shot.
if not defined ROLE_GIVEN (
    set "DO_LISTENER=1"
    set "DO_MONITOR=1"
)

set "ROLE_LABEL="
if defined DO_LISTENER if defined DO_MONITOR set "ROLE_LABEL=listener + monitor"
if defined DO_LISTENER if not defined DO_MONITOR set "ROLE_LABEL=listener"
if not defined DO_LISTENER if defined DO_MONITOR set "ROLE_LABEL=monitor"

echo.
echo ============================================================
echo   nvmon installer  (%ROLE_LABEL%)
echo ============================================================
echo.

REM ----- NVIDIA CLI tools preflight (warning only) --------------------------
REM These exes ship in bin\ (NVIDIA software, NVIDIA licence terms). The
REM listener needs them; the monitor does not. Warn up front if one has gone
REM missing, so it is obvious before anything is installed.
set "NV_MISSING="
for %%E in (configureMosaic.exe configureDdisplay.exe configureQsync2.exe) do (
    if not exist "%BIN_DIR%\%%E" (
        set "NV_FOUND_SRC="
        if defined NVIDIA_SRC if exist "%NVIDIA_SRC%\%%E" set "NV_FOUND_SRC=1"
        if not defined NV_FOUND_SRC set "NV_MISSING=!NV_MISSING! %%E"
    )
)
if defined NV_MISSING (
    echo WARNING: NVIDIA CLI tools missing from %BIN_DIR%\:
    for %%E in (!NV_MISSING!) do echo            %%E
    echo.
    echo   These normally ship in bin\. Restore them from the nvmon repo:
    echo     configureMosaic.exe   - NVIDIA Mosaic Utility
    echo     configureDdisplay.exe - NVIDIA Direct Display configuration utility
    echo     configureQsync2.exe   - NVIDIA Quadro Sync configuration utility
    echo   or copy your own into bin\ ^(or set NVMON_NVIDIA_TOOLS to a folder
    echo   that holds them^). See README "Bundled NVIDIA tools" for SHA-256s.
    echo   The agent still runs without them; those subsystems report unavailable.
    echo.
)

REM ----- Admin check --------------------------------------------------------
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: install.bat must be Run As Administrator.
    echo Right-click and choose "Run as administrator".
    if not defined QUIET pause
    exit /b 1
)

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>&1
echo. >> "%LOG_FILE%"
echo ======================================================== >> "%LOG_FILE%"
echo [%DATE% %TIME%] install.bat started (role: %ROLE_LABEL%) >> "%LOG_FILE%"

REM =========================================================================
REM  Step 1: enclosed Python
REM =========================================================================
echo [1/4] Enclosed Python runtime

if exist "%PY_EXE%" (
    echo       Found existing enclosed Python at .\python\
    goto :py_installed
)

echo       Downloading Python %PY_VERSION% embeddable ^(~10 MB^)...
echo [%DATE% %TIME%] downloading %PY_EMBED_URL% >> "%LOG_FILE%"

if not exist "%PY_DIR%" mkdir "%PY_DIR%" >nul 2>&1
set "PY_ZIP=%PY_DIR%\python-embed.zip"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; try { Invoke-WebRequest -Uri '%PY_EMBED_URL%' -OutFile '%PY_ZIP%' -UseBasicParsing } catch { Write-Error $_; exit 1 }" >> "%LOG_FILE%" 2>&1
if not exist "%PY_ZIP%" (
    echo       ERROR: Python download failed. See %LOG_FILE%.
    exit /b 2
)

echo       Extracting...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Expand-Archive -Path '%PY_ZIP%' -DestinationPath '%PY_DIR%' -Force } catch { Write-Error $_; exit 1 }" >> "%LOG_FILE%" 2>&1
del /f /q "%PY_ZIP%" >nul 2>&1

if not exist "%PY_EXE%" (
    echo       ERROR: extraction failed. See %LOG_FILE%.
    exit /b 2
)

REM Patch the embeddable ._pth so site-packages and the project root are importable.
for %%F in ("%PY_DIR%\python*._pth") do set "PTH_FILE=%%F"
if defined PTH_FILE (
    powershell -NoProfile -Command "(Get-Content '%PTH_FILE%') -replace '^#import site','import site' | Set-Content '%PTH_FILE%'" >> "%LOG_FILE%" 2>&1
    REM Prepend `..` (relative to python\) so the enclosed Python finds
    REM the `nvmon` package no matter where this folder gets copied to.
    powershell -NoProfile -Command "$s = \"..`r`n\" + (Get-Content -Raw '%PTH_FILE%'); Set-Content -Path '%PTH_FILE%' -Value $s -NoNewline" >> "%LOG_FILE%" 2>&1
)

echo       Installing pip...
set "GETPIP=%PY_DIR%\get-pip.py"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; try { Invoke-WebRequest -Uri '%GETPIP_URL%' -OutFile '%GETPIP%' -UseBasicParsing } catch { Write-Error $_; exit 1 }" >> "%LOG_FILE%" 2>&1
"%PY_EXE%" "%GETPIP%" --no-warn-script-location >> "%LOG_FILE%" 2>&1
del /f /q "%GETPIP%" >nul 2>&1

echo       Enclosed Python ready.

:py_installed

REM =========================================================================
REM  Step 2: dependencies
REM =========================================================================
echo.
echo [2/4] Installing Python dependencies
"%PY_EXE%" -m pip install --upgrade pip --no-warn-script-location >> "%LOG_FILE%" 2>&1
"%PY_EXE%" -m pip install -r "%SCRIPT_DIR%\requirements.txt" --no-warn-script-location >> "%LOG_FILE%" 2>&1
if %errorlevel% neq 0 (
    echo       WARNING: pip install had errors. See %LOG_FILE%.
) else (
    echo       Dependencies installed.
)

REM =========================================================================
REM  Step 3: firewall — common (mDNS) + per-role TCP rules
REM =========================================================================
echo.
echo [3/4] Firewall rules
call :fw_udp "nvmon mDNS"     5353
if defined DO_LISTENER (
    call :fw_tcp "nvmon agent"    9998
    call :fw_tcp "nvmon launcher" 9996
)
if defined DO_MONITOR (
    call :fw_tcp "nvmon controller" 9997
)

REM =========================================================================
REM  Step 4: per-role configuration
REM =========================================================================
echo.
echo [4/4] Role-specific setup

if defined DO_LISTENER call :setup_listener
if defined DO_MONITOR  call :setup_monitor

echo.
echo ============================================================
echo   install.bat complete  (%ROLE_LABEL%)
echo.
if defined DO_LISTENER (
    echo   Listener:
    echo     - Scheduled Task "nvmon Listener" auto-runs at logon, elevated, no UAC.
    echo     - Double-click run_listener.bat to start now.
    echo     - kill_listener.bat stops it.
)
if defined DO_MONITOR (
    echo   Monitor:
    echo     - Double-click "nvmon Monitor" on the Public Desktop.
    echo     - Closing the window stops the controller. kill_monitor.bat is a safety net.
    echo     - Controller runs NON-elevated — no UAC after this install.
)
echo ============================================================
echo.

REM Kick off the listener right away so the operator doesn't have to log out.
if defined DO_LISTENER if not defined QUIET (
    set /p "RUN_NOW=Start the listener now? [Y/n] "
    if /I not "!RUN_NOW!"=="n" (
        schtasks /Run /TN "nvmon Listener" >nul 2>&1
        if !errorlevel! equ 0 (
            echo       [OK] task started — check http://%COMPUTERNAME%:9998/health in a moment
        ) else (
            echo       [WARN] schtasks /Run failed; start manually with run_listener.bat
        )
    )
)

if not defined QUIET pause
exit /b 0


REM =========================================================================
REM  Listener role: NVIDIA tools + scheduled task
REM =========================================================================
:setup_listener
echo.
echo --- listener: NVIDIA CLI tools ---
if not exist "%BIN_DIR%" mkdir "%BIN_DIR%" >nul 2>&1
call :need_nvidia configureMosaic.exe
call :need_nvidia configureDdisplay.exe
call :need_nvidia configureQsync2.exe

if defined CONTROLLER_URL (
    echo.
    echo --- listener: push-registration target ---
    echo       baking controller_url=%CONTROLLER_URL% into nvmon.toml
    "%PY_EXE%" -c "import sys; from nvmon.config import set_agent_setting; p = set_agent_setting('controller_url', sys.argv[1]); print('       wrote', p)" "%CONTROLLER_URL%" >> "%LOG_FILE%" 2>&1
    if errorlevel 1 (
        echo       [WARN] set_agent_setting failed — see %LOG_FILE%
    ) else (
        echo       [OK] agent will POST /register to %CONTROLLER_URL% on boot
    )
)

echo.
echo --- listener: Scheduled Task (logon, highest privileges, no UAC prompt) ---
REM A Startup-folder shortcut would re-prompt UAC every time, which makes the
REM listener never come up automatically on render nodes. Task Scheduler with
REM "highest privileges" + a per-user principal is the documented Windows path
REM to skip the prompt while keeping the task elevated.
set "RUN_BAT=%SCRIPT_DIR%\run_listener.bat"
set "TASK_NAME=nvmon Listener"

schtasks /Query /TN "%TASK_NAME%" >nul 2>&1
if %errorlevel% equ 0 (
    schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>&1
    echo       removed previous task entry
)
set "LEGACY_SHORTCUT=%ProgramData%\Microsoft\Windows\Start Menu\Programs\StartUp\nvmon Listener.lnk"
if exist "%LEGACY_SHORTCUT%" (
    del /q "%LEGACY_SHORTCUT%" >nul 2>&1
    echo       removed legacy Startup-folder shortcut
)
REM Clear remove_startup.bat's sentinel — running install.bat --listener is
REM the explicit "I want this back at logon" signal, so it wins over the file.
if exist "%SCRIPT_DIR%\.no_autostart" (
    del /q "%SCRIPT_DIR%\.no_autostart" >nul 2>&1
    echo       cleared .no_autostart sentinel (auto-start re-enabled)
)

REM /RU "<user>" + /RL HIGHEST = same user can `schtasks /Run` it without UAC,
REM but the task itself launches with the user's full unfiltered admin token.
set "TASK_USER=%USERDOMAIN%\%USERNAME%"
schtasks /Create /TN "%TASK_NAME%" ^
    /TR "cmd.exe /c \"%RUN_BAT%\"" ^
    /SC ONLOGON ^
    /RU "%TASK_USER%" ^
    /RL HIGHEST ^
    /F >> "%LOG_FILE%" 2>&1
set "RC=%errorlevel%"
if %RC% equ 0 (
    echo       [OK] scheduled task '%TASK_NAME%' created
    echo       [OK] owner: %TASK_USER%   ^| /RL HIGHEST   ^| /SC ONLOGON
) else (
    echo       [FAIL] schtasks /Create returned errorlevel %RC% — see %LOG_FILE%
    echo       [FAIL] manual command:
    echo              schtasks /Create /TN "%TASK_NAME%" /TR "cmd.exe /c \"%RUN_BAT%\"" /SC ONLOGON /RU "%TASK_USER%" /RL HIGHEST /F
)
exit /b 0


REM =========================================================================
REM  Monitor role: password + ACL + desktop shortcut
REM =========================================================================
:setup_monitor
echo.
echo --- monitor: Controller password ---
if not exist "%SECRETS_DIR%" mkdir "%SECRETS_DIR%" >nul 2>&1

if defined QUIET (
    if not exist "%PASSWORD_FILE%" (
        type nul > "%PASSWORD_FILE%"
        echo       --quiet: empty password file created — controller will run NO-AUTH.
    ) else (
        echo       --quiet: keeping existing password file.
    )
    goto :password_done
)

if exist "%PASSWORD_FILE%" (
    echo       Found existing password file. Press ENTER to keep it,
    set "pw="
    set /p "pw=or type a new password to replace it: "
    if not "!pw!"=="" (
        > "%PASSWORD_FILE%" echo(!pw!
        echo       Password updated.
    ) else (
        echo       Keeping existing password.
    )
    goto :password_done
)

echo       The dashboard can be password-protected.
echo       Leave blank for NO-AUTH ^(fine on a trusted LAN^).
set "pw="
set /p "pw=Password (blank for no-auth): "
if "!pw!"=="" (
    type nul > "%PASSWORD_FILE%"
    echo       NO-AUTH mode active.
) else (
    > "%PASSWORD_FILE%" echo(!pw!
    echo       Password saved.
)

:password_done
icacls "%SECRETS_DIR%" /inheritance:r >nul 2>&1
icacls "%SECRETS_DIR%" /grant:r "%USERNAME%:(OI)(CI)F" >nul 2>&1
icacls "%SECRETS_DIR%" /grant:r "SYSTEM:(OI)(CI)F" >nul 2>&1
icacls "%SECRETS_DIR%" /grant:r "Administrators:(OI)(CI)F" >nul 2>&1

echo.
echo --- monitor: Public Desktop shortcut ---
set "DESKTOP=%PUBLIC%\Desktop"
if not exist "%DESKTOP%" (
    echo       ERROR: Public Desktop not found: %DESKTOP%
    exit /b 1
)
set "SHORTCUT=%DESKTOP%\nvmon Monitor.lnk"
set "MON_BAT=%SCRIPT_DIR%\run_monitor.bat"
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $s = (New-Object -ComObject WScript.Shell).CreateShortcut('%SHORTCUT%'); $s.TargetPath='%MON_BAT%'; $s.WorkingDirectory='%SCRIPT_DIR%'; $s.Description='nvmon monitor (controller + dashboard)'; $s.Save() } catch { Write-Host $_.Exception.Message; exit 1 }" >> "%LOG_FILE%" 2>&1
if exist "%SHORTCUT%" (
    echo       [OK] %SHORTCUT%
) else (
    echo       [FAIL] shortcut not created — see %LOG_FILE%
)
exit /b 0


REM =========================================================================
REM  Subroutines
REM =========================================================================

:need_nvidia
set "EXE_NAME=%~1"
if exist "%BIN_DIR%\%EXE_NAME%" (
    echo       [OK]      %EXE_NAME% already in bin\
    exit /b 0
)
if defined NVIDIA_SRC if exist "%NVIDIA_SRC%\%EXE_NAME%" (
    copy /y "%NVIDIA_SRC%\%EXE_NAME%" "%BIN_DIR%\%EXE_NAME%" >nul 2>&1
    if !errorlevel! equ 0 (
        echo       [COPIED]  %EXE_NAME% from %NVIDIA_SRC%
        exit /b 0
    )
)
echo       [MISSING] %EXE_NAME% - restore it to bin\ from the nvmon repo ^(see README "Bundled NVIDIA tools"^)
exit /b 0

:fw_tcp
set "RULE_NAME=%~1"
set "PORT=%~2"
netsh advfirewall firewall show rule name="%RULE_NAME%" >nul 2>&1
if %errorlevel% equ 0 (
    REM Older installs scoped the rule to private,domain. Control-Net has no gateway, so
    REM Windows files it as "Unidentified network" = Public and the rule never matched.
    netsh advfirewall firewall set rule name="%RULE_NAME%" new profile=any >nul 2>&1
    echo       [TCP %PORT%] %RULE_NAME% already present ^(now on all profiles^)
    exit /b 0
)
netsh advfirewall firewall add rule name="%RULE_NAME%" dir=in action=allow protocol=TCP localport=%PORT% profile=any >nul 2>&1
if %errorlevel% equ 0 (
    echo       [TCP %PORT%] %RULE_NAME% added
) else (
    echo       [TCP %PORT%] %RULE_NAME% FAILED
)
exit /b 0

:fw_udp
set "RULE_NAME=%~1"
set "PORT=%~2"
netsh advfirewall firewall show rule name="%RULE_NAME%" >nul 2>&1
if %errorlevel% equ 0 (
    netsh advfirewall firewall set rule name="%RULE_NAME%" new profile=any >nul 2>&1
    echo       [UDP %PORT%] %RULE_NAME% already present ^(now on all profiles^)
    exit /b 0
)
netsh advfirewall firewall add rule name="%RULE_NAME%" dir=in action=allow protocol=UDP localport=%PORT% profile=any >nul 2>&1
if %errorlevel% equ 0 (
    echo       [UDP %PORT%] %RULE_NAME% added
) else (
    echo       [UDP %PORT%] %RULE_NAME% FAILED
)
exit /b 0


:show_help
echo.
echo nvmon unified installer
echo.
echo Usage:
echo   install.bat                Install BOTH roles on this box (interactive).
echo   install.bat --listener     Render-node side only (agent + launcher).
echo   install.bat --monitor      Central-box side only (controller + dashboard).
echo   install.bat --both         Same as default.
echo   install.bat --quiet        No prompts; safe for unattended bootstrap.
echo   install.bat --controller URL  Listener-only: bake controller URL into
echo                                 nvmon.toml so the agent POSTs /register
echo                                 on boot. Example:
echo                                   install.bat --listener --controller http://192.0.2.10:9997
echo   install.bat --help         Show this help.
echo.
echo Must be run as Administrator. Idempotent — rerun to repair, rotate
echo the controller password, or pick up new requirements. Installs
echo entirely inside this folder (Python in .\python\).
echo.
exit /b 0
