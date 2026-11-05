@echo off
setlocal
title nvmon - update site
REM =========================================================================
REM  Update an EXISTING nvmon site from THIS (newer) NVMON copy.
REM
REM  Run it from the new copy (e.g. a fresh clone of the repo):
REM     update_site.bat                     asks which NVMON folder to update
REM     update_site.bat E:\NVMON            update that folder
REM     update_site.bat E:\NVMON --dry-run  show the plan, change nothing
REM
REM  It asks what to update:
REM     1) monitor only         the monitor + the new desktop app; listeners
REM                             keep their current version
REM     2) monitor + listeners  also pushes to each listener's NVMON share
REM                             (\\<host>\NVMON by default; --share to change)
REM     3) this box is a listener
REM
REM  Never touches the site's own data (nvmon.toml, secrets, your profiles,
REM  EDIDs, logs, python\). Backs up every file it overwrites to
REM  backup\update-<stamp>\ with a ROLLBACK.txt. Each step says whether it
REM  is safe, a brief blip, or changes files, before asking. Details:
REM  bats\update_site.py.
REM =========================================================================
set "SRC=%~dp0"
if "%SRC:~-1%"=="\" set "SRC=%SRC:~0,-1%"
set "TARGET=%~1"
if not defined TARGET set /p "TARGET=Existing NVMON folder to update (e.g. E:\NVMON): "
if not defined TARGET (
    echo No folder given.
    pause
    exit /b 2
)
set "TARGET=%TARGET:"=%"

REM Python: this copy's if it has one, else the existing install's.
set "PY=%SRC%\python\python.exe"
if not exist "%PY%" set "PY=%TARGET%\python\python.exe"
if not exist "%PY%" (
    echo Neither this copy nor "%TARGET%" has python\python.exe.
    echo Run bats\install.bat on the target box first, then try again.
    pause
    exit /b 2
)

"%PY%" -I "%SRC%\bats\update_site.py" --source "%SRC%" --target "%TARGET%" %2 %3 %4 %5 %6
set "RC=%errorlevel%"
echo.
pause
exit /b %RC%
