@echo off
REM =========================================================================
REM  nvmon app -- the dashboard as a desktop app, on any Windows machine.
REM
REM  Double-click. No install, no admin, no firewall prompt. It runs its own
REM  local controller (127.0.0.1 only), opens the dashboard in its own
REM  window (move / resize it like any app), and shows a floating bubble with
REM  "online/total" nodes while that window is minimised or closed.
REM  Click the bubble to open the dashboard, drag it anywhere, right-click it
REM  for Start with Windows / Quit. Launching again just reopens the window.
REM
REM  Needs: this NVMON folder (python\ + nvmon\ + nvmon.toml) and the
REM  machine on Control-Net so it can reach the listeners. Edge or Chrome.
REM
REM  Options:  --discover   also find listeners by mDNS (may ask firewall once)
REM            --no-window  start as the bubble only
REM  State + log: %LOCALAPPDATA%\nvmon-app
REM =========================================================================
cd /d "%~dp0"
if not exist "%~dp0python\pythonw.exe" (
    echo nvmon app: python\pythonw.exe not found next to this script.
    echo Copy the whole NVMON folder, not just this file.
    pause
    exit /b 1
)
start "" "%~dp0python\pythonw.exe" -m nvmon app %*
