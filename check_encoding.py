"""Проверка, что build_exe.py переживает cp1252-консоль, как на CI."""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
GUARD = (
    "import sys\n"
    "[s.reconfigure(encoding='utf-8', errors='replace') "
    "for s in (sys.stdout, sys.stderr)]\n"
    "print('Запуск PyInstaller…')\n"
)
BARE = "print('Запуск PyInstaller…')\n"

env = {"PYTHONIOENCODING": "cp1252", "PATH": r"C:\Windows\System32"}


def run(code: str) -> tuple[int, str]:
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env)
    return r.returncode, r.stderr.decode("utf-8", "replace").strip()


rc, err = run(BARE)
print(f"без принудительной UTF-8 : код {rc}  {'ПАДАЕТ' if rc else 'ок'}")
if err:
    print(f"   {err.splitlines()[-1][:70]}")

rc, err = run(GUARD)
print(f"с принудительной UTF-8    : код {rc}  {'ок' if rc == 0 else 'ПАДАЕТ'}")

# build_exe.py не должен запускать сборку при импорте
r = subprocess.run(
    [sys.executable, "-c", "import build_exe"],
    capture_output=True,
    cwd=str(HERE),
)
imported = r.returncode == 0
print(
    "import build_exe          : "
    + ("ок, сборка не запустилась" if imported else "ПРОБЛЕМА")
)

sys.exit(0 if (rc == 0 and imported) else 1)
