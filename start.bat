@echo off
setlocal

rem --- locate python: py launcher, then known install path, then PATH ---
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PY=%LocalAppData%\Programs\Python\Python312\python.exe"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if not defined PY goto :nopython

rem --- force UTF-8 so Cyrillic output is readable ---
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

cd /d "%~dp0"
%PY% twitch_drops.py %*
set "EXITCODE=%ERRORLEVEL%"
echo.
if not "%EXITCODE%"=="0" echo Program exited with code %EXITCODE%.
pause
exit /b %EXITCODE%

:nopython
echo.
echo   Python not found. Install it from https://www.python.org/downloads/
echo   and tick "Add Python to PATH" during setup.
echo.
pause
exit /b 1
