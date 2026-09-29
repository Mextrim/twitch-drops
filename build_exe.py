# -*- mode: python ; coding: utf-8 -*-
"""Сборка Twitch Drops в один .exe через PyInstaller."""

import subprocess
import sys
from pathlib import Path

# На CI-консоли кодировка по умолчанию cp437/cp1252, и кириллица в выводе
# роняет сборку с UnicodeEncodeError. Принудительно переключаем на UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).parent
NAME = "TwitchDrops"


def main() -> int:
    # ui/ кладём внутрь .exe: код ищет её через sys._MEIPASS
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
        # Эти модули не нужны, но PyInstaller тянет их через urllib3/certifi —
        # исключаем, чтобы не раздувать .exe.
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
        return code

    exe = ROOT / "dist" / (NAME + ".exe")
    if not exe.exists():
        print("Сборка не создала .exe", file=sys.stderr)
        return 1

    print(f"\nГотово: {exe}  ({exe.stat().st_size / 1024 / 1024:.1f} МБ)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
