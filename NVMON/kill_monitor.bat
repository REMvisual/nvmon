@echo off
setlocal enabledelayedexpansion
title nvmon - kill monitor

REM =========================================================================
REM  Kill the monitor: controller (TCP 9997) + its run_monitor.bat console.
REM
REM  Closing the run_monitor.bat terminal already stops the controller.
REM  This script is the safety net for orphaned processes AND the way to
REM  close the window when the user can't reach it (e.g. on a remote box).
REM
REM  Two kill paths, in order:
REM    1. Window-title sweep: taskkill /F /T any cmd.exe whose title is
REM       "nvmon - monitor". /T also takes the python child with it, so
REM       both the dashboard process AND the host console close cleanly
REM       — the original kill_monitor only killed Python, leaving the
REM       cmd window stuck on "Press any key" because Python exited rc!=0.
REM    2. Port-based fallback: anything still listening on TCP 9997 gets
REM       taskkill'd by PID. Catches orphans started outside run_monitor.
REM
REM  No UAC: the controller runs as the calling user (no admin needed),
REM  so taskkill from the same user works without elevation.
REM =========================================================================

if /I "%~1"=="--help" goto :show_help
if /I "%~1"=="-h"     goto :show_help
if /I "%~1"=="/?"     goto :show_help

REM ----- Step 1: kill the run_monitor.bat window by title ------------------
REM /T = also kill child processes (the python -m nvmon controller)
REM /FI WINDOWTITLE matches the title set at the top of run_monitor.bat.
REM Trailing " - cmd*" wildcard catches the suffix Windows sometimes appends.
set "KILLED_TITLE=0"
tasklist /FI "WINDOWTITLE eq nvmon - monitor*" 2>nul | findstr /I "cmd.exe" >nul
if %errorlevel% equ 0 (
    taskkill /F /T /FI "WINDOWTITLE eq nvmon - monitor*" >nul 2>&1
    if !errorlevel! equ 0 (
        echo [monitor] closed run_monitor.bat console window ^(and its child controller^).
        set "KILLED_TITLE=1"
    )
)

REM ----- Step 2: port-based fallback for orphans ---------------------------
REM Wait briefly for the title-kill to release the port; if anything is
REM still listening on 9997, it's an orphan started outside run_monitor.
if "%KILLED_TITLE%"=="1" timeout /t 1 >nul

netstat -ano -p TCP | findstr /R /C:":9997 .*LISTENING" >nul 2>&1
if %errorlevel% equ 0 (
    call :kill_port 9997 "controller"
) else (
    if "%KILLED_TITLE%"=="0" echo [controller] nothing listening on TCP 9997.
)

echo.
echo kill_monitor.bat done.
exit /b 0


:kill_port
set "PORT=%~1"
set "LABEL=%~2"

for /f "tokens=5" %%p in ('netstat -ano -p TCP ^| findstr /R /C:":%PORT% .*LISTENING"') do (
    if not "%%p"=="0" set "PID_%%p=1"
)

set "PIDS="
for /f "tokens=1,2 delims==" %%a in ('set PID_ 2^>nul') do (
    set "ENTRY=%%a"
    set "ENTRY=!ENTRY:PID_=!"
    set "PIDS=!PIDS! !ENTRY!"
)

if "%PIDS%"=="" (
    exit /b 0
)

for %%x in (%PIDS%) do (
    echo [%LABEL%] killing orphan PID %%x on port %PORT%
    taskkill /F /T /PID %%x >nul 2>&1
    if !errorlevel! neq 0 echo [%LABEL%]   taskkill failed for PID %%x
)

for /f "tokens=1 delims==" %%a in ('set PID_ 2^>nul') do set "%%a="
exit /b 0


:show_help
echo.
echo nvmon kill_monitor.bat
echo.
echo Usage:
echo   kill_monitor.bat        Stop the controller AND close run_monitor.bat.
echo   kill_monitor.bat --help Show this help.
echo.
echo Closes any cmd window titled "nvmon - monitor" (which takes the python
echo child with it via /T), then sweeps TCP 9997 for orphans. The controller
echo runs non-elevated so this script does not need admin.
echo.
exit /b 0
