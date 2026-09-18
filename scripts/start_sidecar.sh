#!/usr/bin/env bash
# Start OCR sidecar bound to Library data root (cloud phase defaults in settings.json).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_ROOT="${OCR_DATA_ROOT:-/root/autodl-tmp/Library}"
export OCR_DATA_ROOT="$DATA_ROOT"
export OCR_PYTHON="${OCR_PYTHON:-/root/miniconda3/envs/OCR/bin/python}"
cd "$ROOT"
# shellcheck disable=SC1091
source /root/miniconda3/etc/profile.d/conda.sh
conda activate OCR
exec python sidecar/main.py --host 127.0.0.1 --port 18765 --data-root "$DATA_ROOT"
