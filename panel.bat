@echo off
rem Запускает панель Twitch Drops из собранного .exe
cd /d "%~dp0"

if exist "TwitchDrops.exe" (
    start "" "TwitchDrops.exe" --gui
    exit /b 0
)

rem .exe нет — падаем обратно на запуск через Python
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PY=%LocalAppData%\Programs\Python\Python312\python.exe"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if not defined PY (
    echo.
    echo   Python not found, and TwitchDrops.exe is missing.
    echo   Run: python build_exe.py
    echo.
    pause
    exit /b 1
)
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
%PY% twitch_drops.py --gui %*
pause
