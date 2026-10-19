"""Install the source toolkit with the selected interpreter; no browser download."""
import os
from pathlib import Path
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
# Separate attempts, never extra-index; global pip configuration stays intact.
INDEXES = (None, "https://pypi.org/simple", "https://pypi.tuna.tsinghua.edu.cn/simple")


def run(args, *, cwd=ROOT):
    return subprocess.run([sys.executable, *args], cwd=cwd).returncode == 0


def install(args, log):
    for index in INDEXES:
        print("[install-windows] Package source: " + (index or "pip configuration"), flush=True)
        options = ["--index-url", index] if index else []
        if run(["-m", "pip", "--disable-pip-version-check", "--no-input",
                "--log", str(log), "install", "--timeout", "60", "--retries", "3",
                *options, *args]):
            return True
    return False


def main():
    if sys.version_info < (3, 10) or struct.calcsize("P") != 8:
        print("[install-windows] ERROR: 64-bit Python 3.10+ required.")
        return 1
    print("[install-windows] Python: " + sys.executable, flush=True)
    log = ROOT / "install-windows.log"
    print("[install-windows] Log: " + str(log), flush=True)
    if not run(["-m", "pip", "--version"]):
        if not run(["-m", "ensurepip", "--upgrade"]):
            print("[install-windows] ERROR: pip unavailable; repair your Python installation.")
            return 1
    # Explicit bootstrap avoids a second hidden build-isolation download.
    if not install(["pip>=23", "setuptools>=68", "wheel"], log):
        print("[install-windows] ERROR: Build dependencies failed. See " + str(log))
        return 1
    if not install(["--no-build-isolation", "-e", str(ROOT)], log):
        print("[install-windows] ERROR: Toolkit installation failed. See " + str(log))
        return 1
    # Check this interpreter outside the source cwd, not a stale PATH executable.
    if not run(["-c", "from patchright.sync_api import sync_playwright; "
                "p = sync_playwright().start(); p.stop()"], cwd=ROOT.parent):
        print("[install-windows] ERROR: Browser client failed to start.")
        return 1
    if not run(["-m", "boss_hr", "--help"], cwd=ROOT.parent):
        print("[install-windows] ERROR: CLI verification failed.")
        return 1
    if not any((Path(p) / "boss-hr.exe").is_file()
               for p in os.environ.get("PATH", "").split(os.pathsep) if p):
        print('[install-windows] CLI may not be on PATH. Use: "' + sys.executable + '" -m boss_hr')
    print("[install-windows] OK. Keep source at: " + str(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
