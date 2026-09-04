"""Build ocr-sidecar.exe with PyInstaller (one-folder)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIDECAR = ROOT / "sidecar"
OUT = ROOT / "resources" / "ocr-sidecar"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--name",
        "ocr-sidecar",
        "--onedir",
        "--clean",
        "--noconfirm",
        "--paths",
        str(SIDECAR),
        "--collect-submodules",
        "ocr_app",
        "--hidden-import",
        "uvicorn.logging",
        "--hidden-import",
        "uvicorn.loops",
        "--hidden-import",
        "uvicorn.loops.auto",
        "--hidden-import",
        "uvicorn.protocols",
        "--hidden-import",
        "uvicorn.protocols.http",
        "--hidden-import",
        "uvicorn.protocols.http.auto",
        "--hidden-import",
        "uvicorn.protocols.websockets",
        "--hidden-import",
        "uvicorn.protocols.websockets.auto",
        "--hidden-import",
        "uvicorn.lifespan",
        "--hidden-import",
        "uvicorn.lifespan.on",
        "--hidden-import",
        "aiosqlite",
        "--hidden-import",
        "greenlet",
        "--distpath",
        str(OUT.parent),
        "--workpath",
        str(ROOT / "build" / "pyinstaller"),
        "--specpath",
        str(ROOT / "build" / "pyinstaller"),
        str(SIDECAR / "main.py"),
    ]
    print("Running:", " ".join(cmd))
    subprocess.check_call(cmd, cwd=str(SIDECAR))
    print(f"Built: {OUT / 'ocr-sidecar.exe'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
