@echo off
setlocal enabledelayedexpansion
title nvmon - kill listener

REM =========================================================================
REM  Kill the listener: agent (TCP 9998) + launcher (TCP 9996).
REM
REM  Works by reading `netstat -ano`, matching the listening port,
REM  and running `taskkill /F /T /PID <pid>`. Survives renamed processes
REM  and orphaned children.
REM =========================================================================

if /I "%~1"=="--help" goto :show_help
if /I "%~1"=="-h"     goto :show_help
if /I "%~1"=="/?"     goto :show_help

REM ----- Try the no-UAC stop paths first -----------------------------------
REM (a) Scheduled Task: if the listener came up via "nvmon Listener", the
REM     user owns the task and `schtasks /End` works without elevation.
REM (b) Window-title sweep: any cmd.exe titled "nvmon - listener" gets
REM     /F /T'd, which closes the host console AND its python child. That
REM     fixes the "kill happens but the window stays at Press-any-key"
REM     symptom — we kill the cmd, not just the python.
REM Then verify nothing's still listening; if so, fall through to the
REM elevated taskkill path that handles orphans.
set "TASK_NAME=nvmon Listener"
schtasks /Query /TN "%TASK_NAME%" >nul 2>&1
if %errorlevel% equ 0 (
    schtasks /End /TN "%TASK_NAME%" >nul 2>&1
)

tasklist /FI "WINDOWTITLE eq nvmon - listener*" 2>nul | findstr /I "cmd.exe" >nul
if %errorlevel% equ 0 (
    taskkill /F /T /FI "WINDOWTITLE eq nvmon - listener*" >nul 2>&1
)

timeout /t 1 >nul
netstat -ano -p TCP | findstr /R /C:":9998 .*LISTENING" >nul 2>&1
if %errorlevel% neq 0 (
    netstat -ano -p TCP | findstr /R /C:":9996 .*LISTENING" >nul 2>&1
    if !errorlevel! neq 0 (
        echo [kill_listener] stopped via task / window-title -- no UAC needed.
        exit /b 0
    )
)
echo [kill_listener] something is still listening; falling back to PID-based kill...

REM ----- Auto-elevate -------------------------------------------------------
REM taskkill against an elevated PID needs elevation itself, so make sure
REM we're admin before bothering with netstat probing.
fltmc >nul 2>&1
if %errorlevel% neq 0 (
    echo [kill_listener] need admin to taskkill an elevated PID -- accept UAC.
    powershell -NoProfile -Command ^
        "try { Start-Process -FilePath '%~f0' -Verb RunAs -WorkingDirectory '%~dp0' -ErrorAction Stop } " ^
        "catch { Write-Host ('elevation failed: ' + $_.Exception.Message) -ForegroundColor Red; exit 1 }"
    if errorlevel 1 pause
    exit /b 0
)

call :kill_port 9998 "agent"
call :kill_port 9996 "launcher"

echo.
echo kill_listener.bat done.
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
    echo [%LABEL%] nothing listening on TCP %PORT%.
    exit /b 0
)

for %%x in (%PIDS%) do (
    echo [%LABEL%] killing PID %%x on port %PORT%
    taskkill /F /T /PID %%x >nul 2>&1
    if !errorlevel! neq 0 echo [%LABEL%]   taskkill failed for PID %%x
)

for /f "tokens=1 delims==" %%a in ('set PID_ 2^>nul') do set "%%a="
exit /b 0


:show_help
echo.
echo nvmon kill_listener.bat
echo.
echo Usage:
echo   kill_listener.bat        Stop agent + launcher on this node.
echo   kill_listener.bat --help Show this help.
echo.
echo Kills whatever is listening on TCP 9998 (agent) and TCP 9996 (launcher).
echo Use kill_monitor.bat for the controller.
echo.
exit /b 0
