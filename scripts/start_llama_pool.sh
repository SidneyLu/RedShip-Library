#!/usr/bin/env bash
# Start N CUDA llama-cpp-python instances + a round-robin proxy on :8080.
set -euo pipefail
APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_ROOT="${OCR_DATA_ROOT:-/root/autodl-tmp/Library}"
MODEL_DIR="${MODEL_DIR:-/root/autodl-tmp/models/Qwen3.5-4B-GGUF}"
N="${LLAMA_N:-6}"
BASE_PORT="${LLAMA_BASE_PORT:-8081}"
LB_PORT="${LLAMA_LB_PORT:-8080}"
N_CTX="${N_CTX:-8192}"
PYTHON_BIN="${OCR_PYTHON:-/root/miniconda3/envs/OCR/bin/python}"
PID_DIR="$DATA_ROOT/run"
LOG_DIR="$DATA_ROOT/logs"
LOG="$LOG_DIR/llama_server.log"
mkdir -p "$PID_DIR" "$LOG_DIR"

export CUDA_HOME="${CUDA_HOME:-/usr/local/cuda-13.0}"
export PATH="$CUDA_HOME/bin:$PATH"
export LD_LIBRARY_PATH="$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}"
# shellcheck disable=SC1091
source /root/miniconda3/etc/profile.d/conda.sh
conda activate OCR

echo "===== LLAMA POOL START $(date -Iseconds) n=$N ctx=$N_CTX =====" >>"$LOG"

backends=()
for i in $(seq 0 $((N - 1))); do
  port=$((BASE_PORT + i))
  echo "starting llama instance $((i + 1))/$N on :$port" | tee -a "$LOG"
  nohup "$PYTHON_BIN" -m llama_cpp.server \
    --model "$MODEL_DIR/Qwen3.5-4B-Q4_K_M.gguf" \
    --model_alias Qwen3.5-4B \
    --clip_model_path "$MODEL_DIR/mmproj-F16.gguf" \
    --host 127.0.0.1 --port "$port" \
    --n_gpu_layers -1 \
    --n_ctx "$N_CTX" \
    --n_batch 256 \
    --interrupt_requests False \
    >>"$LOG" 2>&1 &
  echo $! >"$PID_DIR/llama_$port.pid"
  ok=0
  for _ in $(seq 1 90); do
    if curl -fsS -m 2 "http://127.0.0.1:$port/v1/models" >/dev/null 2>&1; then
      ok=1
      break
    fi
    sleep 1
  done
  if [[ "$ok" != 1 ]]; then
    echo "WARN instance :$port failed to become ready" | tee -a "$LOG"
    continue
  fi
  backends+=("http://127.0.0.1:$port")
  nvidia-smi --query-gpu=memory.used --format=csv,noheader | tee -a "$LOG"
done

if [[ ${#backends[@]} -lt 1 ]]; then
  echo "ERROR: no llama instances started" >&2
  exit 1
fi

IFS=','; export LLAMA_BACKENDS="${backends[*]}"; unset IFS
echo "backends=$LLAMA_BACKENDS" | tee -a "$LOG"

nohup "$PYTHON_BIN" "$APP_ROOT/sidecar/scripts/openai_lb.py" \
  --host 127.0.0.1 --port "$LB_PORT" --backends "$LLAMA_BACKENDS" \
  >>"$LOG_DIR/llama_lb.log" 2>&1 &
echo $! >"$PID_DIR/llama_lb.pid"
for _ in $(seq 1 30); do
  if curl -fsS -m 2 "http://127.0.0.1:$LB_PORT/v1/models" >/dev/null 2>&1; then
    echo "load balancer ready :$LB_PORT n=${#backends[@]}" | tee -a "$LOG"
    exit 0
  fi
  sleep 0.3
done
echo "ERROR: load balancer not ready" >&2
exit 1
