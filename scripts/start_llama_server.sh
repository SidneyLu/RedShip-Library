#!/usr/bin/env bash
# Example local VL server launch for phase-2 inspection reruns (RTX 4090).
# Sidecar expects OpenAI Responses at {base}/responses — use a Responses-compatible
# front-end, or a gateway in front of llama-server / llama-cpp-python.
set -euo pipefail
MODEL_DIR="${MODEL_DIR:-/root/autodl-tmp/models/Qwen3.5-4B-GGUF}"
PORT="${PORT:-8080}"
NP="${NP:-4}"

if [[ -n "${LLAMA_SERVER:-}" && -x "$LLAMA_SERVER" ]]; then
  BIN="$LLAMA_SERVER"
elif [[ -x /root/autodl-tmp/llama.cpp/build/bin/llama-server ]]; then
  BIN=/root/autodl-tmp/llama.cpp/build/bin/llama-server
elif command -v llama-server >/dev/null 2>&1; then
  BIN="$(command -v llama-server)"
else
  echo "llama-server not found. Build llama.cpp with CUDA, or set LLAMA_SERVER=/path/to/llama-server" >&2
  echo "Model ready at: $MODEL_DIR" >&2
  exit 1
fi

exec "$BIN" \
  -m "$MODEL_DIR/Qwen3.5-4B-Q4_K_M.gguf" \
  --mmproj "$MODEL_DIR/mmproj-F16.gguf" \
  --host 127.0.0.1 --port "$PORT" \
  -ngl 99 \
  -c 8192 \
  -np "$NP" \
  --parallel "$NP"
