"""OCR Library sidecar entrypoint."""
from __future__ import annotations

import argparse
import sys

import uvicorn

from ocr_app.settings_store import bootstrap_from_disk


def main() -> int:
    parser = argparse.ArgumentParser(description="OCR Library sidecar")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--data-root", default=None)
    args = parser.parse_args()

    if args.data_root:
        import os

        os.environ["OCR_DATA_ROOT"] = args.data_root

    bootstrap_from_disk()

    from ocr_app.api.server import app

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
