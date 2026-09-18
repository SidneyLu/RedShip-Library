#!/usr/bin/env bash
# llama-cpp-python OpenAI-compatible chat server for local VL OCR reruns (CUDA).
set -euo pipefail
MODEL_DIR="${MODEL_DIR:-/root/autodl-tmp/models/Qwen3.5-4B-GGUF}"
PORT="${PORT:-8080}"
export CUDA_HOME="${CUDA_HOME:-/usr/local/cuda-13.0}"
export PATH="$CUDA_HOME/bin:$PATH"
export LD_LIBRARY_PATH="$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}"
# shellcheck disable=SC1091
source /root/miniconda3/etc/profile.d/conda.sh
conda activate OCR
exec python -m llama_cpp.server \
  --model "$MODEL_DIR/Qwen3.5-4B-Q4_K_M.gguf" \
  --model_alias Qwen3.5-4B \
  --clip_model_path "$MODEL_DIR/mmproj-F16.gguf" \
  --host 127.0.0.1 --port "$PORT" \
  --n_gpu_layers -1 \
  --n_ctx "${N_CTX:-8192}" \
  --n_batch 512 \
  --interrupt_requests False
