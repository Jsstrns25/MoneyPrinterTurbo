"""Hidden local supervisor; run with .venv/Scripts/pythonw.exe on Windows."""

import subprocess
import sys
import time
from pathlib import Path

from filelock import FileLock, Timeout

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.paths import runtime_root  # noqa: E402


def main():
    data = runtime_root() / "storage" / "argstudio"
    data.mkdir(parents=True, exist_ok=True)
    stop = data / "stop.request"
    if "--stop" in sys.argv:
        stop.touch()
        return
    lock = FileLock(str(data / "windows-supervisor.lock"), timeout=0)
    try:
        with lock, (data / "windows.log").open("a", encoding="utf-8") as log:
            stop.unlink(missing_ok=True)
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            python = (
                str(Path(sys.executable).with_name("python.exe"))
                if sys.platform == "win32"
                else sys.executable
            )
            commands = [
                [
                    python,
                    "-m",
                    "streamlit",
                    "run",
                    str(ROOT / "webui" / "Main.py"),
                    "--server.address=127.0.0.1",
                    "--server.port=8501",
                    "--server.headless=true",
                    "--browser.gatherUsageStats=false",
                ],
                [python, "-m", "app.argstudio", "worker"],
            ]
            children = []
            try:
                children = [
                    subprocess.Popen(
                        cmd, cwd=ROOT, stdout=log, stderr=log, creationflags=flags
                    )
                    for cmd in commands
                ]
                while not stop.exists() and all(
                    child.poll() is None for child in children
                ):
                    time.sleep(2)
            finally:
                for child in children:
                    if child.poll() is None:
                        if sys.platform == "win32":
                            subprocess.run(
                                ["taskkill", "/PID", str(child.pid), "/T", "/F"],
                                stdout=log,
                                stderr=log,
                                creationflags=flags,
                                check=False,
                            )
                        else:
                            child.terminate()
                for child in children:
                    try:
                        child.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        child.kill()
    except Timeout:
        return  # Already running, including from a scheduled task.


if __name__ == "__main__":
    main()
