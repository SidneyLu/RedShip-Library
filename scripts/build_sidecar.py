"""Build ocr-sidecar.exe with PyInstaller (one-folder)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIDECAR = ROOT / "sidecar"
OUT = ROOT / "resources" / "ocr-sidecar"

# Conda/Windows Python extension modules need these native DLLs beside the exe.
# PyInstaller often misses them when building from a conda env.
_REQUIRED_DLLS = (
    "libssl-3-x64.dll",
    "libcrypto-3-x64.dll",
    "sqlite3.dll",
    "ffi.dll",
    # Also required by conda-built stdlib extensions (pyexpat / _lzma / _bz2).
    "libexpat.dll",
    "liblzma.dll",
    "LIBBZ2.dll",
)


def _conda_library_bins() -> list[Path]:
    prefix = Path(sys.prefix)
    candidates = [
        prefix / "Library" / "bin",
        prefix.parent.parent / "Library" / "bin",  # base env next to envs/OCR
    ]
    # Also honor CONDA_PREFIX when running outside the env python path edge-cases.
    conda_prefix = os.environ.get("CONDA_PREFIX")
    if conda_prefix:
        candidates.insert(0, Path(conda_prefix) / "Library" / "bin")
    seen: set[Path] = set()
    out: list[Path] = []
    for p in candidates:
        rp = p.resolve()
        if rp in seen or not rp.is_dir():
            continue
        seen.add(rp)
        out.append(rp)
    return out


def _find_dll(name: str) -> Path:
    for bin_dir in _conda_library_bins():
        candidate = bin_dir / name
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"Required DLL not found: {name}. "
        f"Searched: {[str(p) for p in _conda_library_bins()]}"
    )


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    # Onedir places binaries under _internal when dest is "."
    add_binary_args: list[str] = []
    for name in _REQUIRED_DLLS:
        src = _find_dll(name)
        add_binary_args.extend(["--add-binary", f"{src};."])
        print(f"Bundling DLL: {src}")

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
        "--collect-all",
        "pymupdf",
        "--collect-all",
        "dashscope",
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
        "--hidden-import",
        "httpx",
        "--hidden-import",
        "loguru",
        "--hidden-import",
        "tenacity",
        "--hidden-import",
        "multipart",
        *add_binary_args,
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

    # Fail the build early if conda DLLs did not land in the bundle.
    internal = OUT / "_internal"
    missing = [n for n in _REQUIRED_DLLS if not (internal / n).is_file()]
    if missing:
        raise SystemExit(f"Built sidecar missing required DLLs: {missing}")

    print(f"Built: {OUT / 'ocr-sidecar.exe'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
