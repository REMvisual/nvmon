@echo off
setlocal enabledelayedexpansion
title nvmon - sync presets

REM =========================================================================
REM  Push a profile/preset to every reachable node via the local controller.
REM  Wraps `POST /all/apply` with the password from secrets\controller.password.
REM
REM  Usage:
REM     sync_presets.bat <profile-name>           (apply profile to all)
REM     sync_presets.bat --restore                (restore all to desktop)
REM     sync_presets.bat --group <name> <profile>
REM     sync_presets.bat --list
REM     sync_presets.bat --help
REM
REM  The controller must be running on this box (run_monitor.bat).
REM =========================================================================

if /I "%~1"=="--help" goto :show_help
if /I "%~1"=="-h"     goto :show_help
if /I "%~1"=="/?"     goto :show_help
if "%~1"==""          goto :show_help

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
REM This script lives in bats\ — redirect SCRIPT_DIR to the NVMON product root.
for %%I in ("%SCRIPT_DIR%\..") do set "SCRIPT_DIR=%%~fI"
set "PASSWORD_FILE=%SCRIPT_DIR%\secrets\controller.password"
set "CONTROLLER=http://localhost:9997"

REM ----- Resolve password (empty = no-auth mode) ----------------------------
set "PW="
if exist "%PASSWORD_FILE%" (
    for /f "usebackq delims=" %%L in ("%PASSWORD_FILE%") do if not defined PW set "PW=%%L"
)

REM ----- Dispatch -----------------------------------------------------------
if /I "%~1"=="--list" (
    call :curl_get "/nodes"
    exit /b 0
)

if /I "%~1"=="--restore" (
    echo [sync] restoring every node to the Windows desktop
    call :curl_post "/all/restore" "{}"
    exit /b 0
)

if /I "%~1"=="--group" (
    if "%~2"=="" (
        echo ERROR: --group needs a group name and a profile.
        exit /b 1
    )
    if "%~3"=="" (
        echo ERROR: --group needs a profile after the group name.
        exit /b 1
    )
    set "GROUP=%~2"
    set "PROFILE=%~3"
    echo [sync] applying profile "!PROFILE!" to group "!GROUP!"
    call :curl_post "/group/!GROUP!/apply" "{\"profile\":\"!PROFILE!\"}"
    exit /b 0
)

REM Default: apply named profile to all
set "PROFILE=%~1"
echo [sync] applying profile "%PROFILE%" to every reachable node
call :curl_post "/all/apply" "{\"profile\":\"%PROFILE%\"}"
exit /b 0


REM =========================================================================
REM  Subroutines (use PowerShell for HTTP — works without curl on Win10/11)
REM =========================================================================

:curl_post
set "PATH_=%~1"
set "BODY=%~2"
set "URL=%CONTROLLER%%PATH_%"

if defined PW (
    powershell -NoProfile -Command "try { $h=@{'X-NVMON-Password'='%PW%'; 'Content-Type'='application/json'}; $r=Invoke-RestMethod -Uri '%URL%' -Method Post -Headers $h -Body '%BODY%'; $r | ConvertTo-Json -Depth 6 } catch { Write-Host ('ERROR: ' + $_.Exception.Message); exit 1 }"
) else (
    powershell -NoProfile -Command "try { $h=@{'Content-Type'='application/json'}; $r=Invoke-RestMethod -Uri '%URL%' -Method Post -Headers $h -Body '%BODY%'; $r | ConvertTo-Json -Depth 6 } catch { Write-Host ('ERROR: ' + $_.Exception.Message); exit 1 }"
)
exit /b 0

:curl_get
set "PATH_=%~1"
set "URL=%CONTROLLER%%PATH_%"

if defined PW (
    powershell -NoProfile -Command "try { $h=@{'X-NVMON-Password'='%PW%'}; $r=Invoke-RestMethod -Uri '%URL%' -Method Get -Headers $h; $r | ConvertTo-Json -Depth 6 } catch { Write-Host ('ERROR: ' + $_.Exception.Message); exit 1 }"
) else (
    powershell -NoProfile -Command "try { $r=Invoke-RestMethod -Uri '%URL%' -Method Get; $r | ConvertTo-Json -Depth 6 } catch { Write-Host ('ERROR: ' + $_.Exception.Message); exit 1 }"
)
exit /b 0


:show_help
echo.
echo nvmon sync_presets.bat — push presets to every node via the controller
echo.
echo Usage:
echo   sync_presets.bat ^<profile^>             Apply profile to every reachable node.
echo   sync_presets.bat --restore             Restore every node to the desktop.
echo   sync_presets.bat --group ^<g^> ^<prof^>    Apply profile to one named group.
echo   sync_presets.bat --list                Print the controller's node list.
echo   sync_presets.bat --help                Show this help.
echo.
echo Reads the controller password from secrets\controller.password
echo (empty file = no-auth mode). Targets http://localhost:9997 — the
echo controller (run_monitor.bat) must be running on this box.
echo.
echo Examples:
echo   sync_presets.bat mosaic-2397
echo   sync_presets.bat --group outputs mosaic-24
echo   sync_presets.bat --restore
echo.
exit /b 0
