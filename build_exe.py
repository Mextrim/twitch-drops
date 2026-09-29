# -*- mode: python ; coding: utf-8 -*-
"""Сборка Twitch Drops в один .exe через PyInstaller."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
NAME = "TwitchDrops"

# ui/ кладём рядом с исполняемым файлом: код ищет её через sys._MEIPASS
sep = ";" if sys.platform == "win32" else ":"

args = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--console",
    "--name", NAME,
    "--add-data", f"{ROOT / 'ui'}{sep}ui",
    "--hidden-import", "requests",
    "--exclude-module", "tkinter",
    "--exclude-module", "numpy",
    "--exclude-module", "PIL",
    "--exclude-module", "pytest",
    "--distpath", str(ROOT / "dist"),
    "--workpath", str(ROOT / "build"),
    "--specpath", str(ROOT / "build"),
    str(ROOT / "twitch_drops.py"),
]

print("Запуск PyInstaller…")
print(" ".join(args[3:]), "\n")
code = subprocess.call(args)
if code != 0:
    sys.exit(code)

exe = ROOT / "dist" / (NAME + ".exe")
if not exe.exists():
    sys.exit("Сборка не создала .exe")

print(f"\nГотово: {exe}  ({exe.stat().st_size / 1024 / 1024:.1f} МБ)")
