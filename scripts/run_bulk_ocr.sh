#!/usr/bin/env bash
# RedShip-Library 云端 OCR 批处理：一键启停 sidecar + bulk + monitor
#
# 用法:
#   ./scripts/run_bulk_ocr.sh start      # 启动（默认续跑剩余队列）
#   ./scripts/run_bulk_ocr.sh stop       # 停止 bulk/monitor（默认保留 sidecar）
#   ./scripts/run_bulk_ocr.sh restart    # 停再起
#   ./scripts/run_bulk_ocr.sh status     # 看进程 / health / 文档状态
#   ./scripts/run_bulk_ocr.sh tui        # 前台进度面板（Ctrl+C 只退 TUI）
#   ./scripts/run_bulk_ocr.sh logs       # 跟 bulk 日志
#
# 环境变量可覆盖默认:
#   OCR_DATA_ROOT  OCR_BASE_URL  OCR_PAGE  OCR_DOC  OCR_API  OCR_WORKERS
#   OCR_LLM_PROVIDER  OCR_OPENAI_BASE  OCR_VISION_MODEL
#   OCR_STOP_SIDECAR=1  时 stop/restart 也会停 sidecar
set -euo pipefail

APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_ROOT="${OCR_DATA_ROOT:-/root/autodl-tmp/Library}"
BASE_URL="${OCR_BASE_URL:-http://127.0.0.1:18765}"
CONDA_SH="${CONDA_SH:-/root/miniconda3/etc/profile.d/conda.sh}"
OCR_ENV="${OCR_ENV:-OCR}"
PYTHON_BIN="${OCR_PYTHON:-/root/miniconda3/envs/OCR/bin/python}"

PAGE="${OCR_PAGE:-64}"
DOC="${OCR_DOC:-32}"
API="${OCR_API:-96}"
WORKERS="${OCR_WORKERS:-8}"
LLM_PROVIDER="${OCR_LLM_PROVIDER:-dashscope}"
OPENAI_BASE="${OCR_OPENAI_BASE:-}"
VISION_MODEL="${OCR_VISION_MODEL:-}"

PID_DIR="$DATA_ROOT/run"
LOG_DIR="$DATA_ROOT/logs"
mkdir -p "$PID_DIR" "$LOG_DIR" "$DATA_ROOT"
SIDECAR_PID="$PID_DIR/sidecar.pid"
BULK_PID="$PID_DIR/bulk.pid"
MONITOR_PID="$PID_DIR/monitor.pid"

SIDECAR_LOG="$LOG_DIR/sidecar.log"
BULK_LOG="$LOG_DIR/bulk_ocr.log"
MONITOR_LOG="$LOG_DIR/ocr_monitor.log"

activate_env() {
  # shellcheck disable=SC1090
  source "$CONDA_SH"
  conda activate "$OCR_ENV"
  cd "$APP_ROOT"
}

pid_of_pattern() {
  # Prefer exact long-lived workers; ignore this wrapper script.
  pgrep -f "$1" 2>/dev/null | while read -r pid; do
    local cmd
    cmd="$(ps -o args= -p "$pid" 2>/dev/null || true)"
    [[ "$cmd" == *run_bulk_ocr.sh* ]] && continue
    echo "$pid"
    break
  done
}

is_alive() {
  local pid_file="$1"
  [[ -f "$pid_file" ]] || return 1
  local pid
  pid="$(tr -d ' \n' <"$pid_file" 2>/dev/null || true)"
  [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null
}

sync_pidfile() {
  local pid_file="$1" pattern="$2"
  if is_alive "$pid_file"; then
    return 0
  fi
  local pid
  pid="$(pid_of_pattern "$pattern")"
  if [[ -n "${pid:-}" ]]; then
    echo "$pid" >"$pid_file"
  else
    rm -f "$pid_file"
  fi
}

kill_pidfile() {
  local pid_file="$1" name="$2"
  if is_alive "$pid_file"; then
    local pid
    pid="$(cat "$pid_file")"
    echo "stopping $name pid=$pid"
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.3
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "force kill $name pid=$pid"
      kill -9 "$pid" 2>/dev/null || true
    fi
  fi
  rm -f "$pid_file"
}

# 兜底：按命令行匹配杀掉（无 pid 文件时）
kill_by_pattern() {
  local pat="$1"
  pkill -f "$pat" 2>/dev/null || true
}

health_ok() {
  curl -fsS -m 3 "$BASE_URL/health" >/dev/null 2>&1
}

wait_health() {
  local i
  for i in $(seq 1 40); do
    if health_ok; then
      curl -fsS -m 3 "$BASE_URL/health"
      echo
      return 0
    fi
    sleep 0.5
  done
  echo "ERROR: sidecar health failed after wait: $BASE_URL/health" >&2
  return 1
}

apply_settings() {
  local body
  body="{\"ocr_page_concurrency\":${PAGE},\"ocr_document_concurrency\":${DOC},\"ocr_api_concurrency\":${API},\"ocr_worker_processes\":${WORKERS},\"vision_pdf_dpi\":300,\"llm_provider\":\"${LLM_PROVIDER}\""
  if [[ -n "$VISION_MODEL" ]]; then
    body+=",\"vision_model\":\"${VISION_MODEL}\",\"chat_model\":\"${VISION_MODEL}\",\"openai_vision_model\":\"${VISION_MODEL}\",\"openai_chat_model\":\"${VISION_MODEL}\""
  fi
  if [[ -n "$OPENAI_BASE" ]]; then
    body+=",\"openai_base_url\":\"${OPENAI_BASE}\""
  fi
  body+="}"
  curl -fsS -X PUT "$BASE_URL/settings" \
    -H 'Content-Type: application/json' \
    -d "$body" \
    >/dev/null
  echo "settings applied: page=${PAGE} doc=${DOC} api=${API} workers=${WORKERS} provider=${LLM_PROVIDER} model=${VISION_MODEL:-'(unchanged)'} openai_base=${OPENAI_BASE:-'(unchanged)'}"
}

start_sidecar() {
  if health_ok; then
    echo "sidecar already healthy: $BASE_URL"
    return 0
  fi
  if is_alive "$SIDECAR_PID"; then
    echo "sidecar pid alive but unhealthy; restarting"
    kill_pidfile "$SIDECAR_PID" sidecar
  fi
  activate_env
  echo "===== SIDECAR START $(date -Iseconds) =====" >>"$SIDECAR_LOG"
  nohup "$PYTHON_BIN" sidecar/main.py \
    --host 127.0.0.1 --port 18765 --data-root "$DATA_ROOT" \
    >>"$SIDECAR_LOG" 2>&1 &
  echo $! >"$SIDECAR_PID"
  echo "sidecar started pid=$(cat "$SIDECAR_PID")"
  wait_health
}

start_bulk() {
  if is_alive "$BULK_PID"; then
    echo "bulk already running pid=$(cat "$BULK_PID")"
    return 0
  fi
  kill_by_pattern 'sidecar/scripts/bulk_import_ocr.py'
  activate_env
  echo "===== BULK START $(date -Iseconds) page=$PAGE doc=$DOC api=$API workers=$WORKERS =====" >>"$BULK_LOG"
  nohup "$PYTHON_BIN" -u sidecar/scripts/bulk_import_ocr.py \
    --data-root "$DATA_ROOT" \
    --base-url "$BASE_URL" \
    --ocr-only --skip-probe \
    --page-concurrency "$PAGE" \
    --doc-concurrency "$DOC" \
    --api-concurrency "$API" \
    --workers "$WORKERS" \
    >>"$BULK_LOG" 2>&1 &
  echo $! >"$BULK_PID"
  echo "bulk started pid=$(cat "$BULK_PID") log=$BULK_LOG"
}

start_monitor() {
  if is_alive "$MONITOR_PID"; then
    echo "monitor already running pid=$(cat "$MONITOR_PID")"
    return 0
  fi
  kill_by_pattern 'sidecar/scripts/ocr_monitor.py'
  activate_env
  nohup "$PYTHON_BIN" sidecar/scripts/ocr_monitor.py \
    --interval 60 --data-root "$DATA_ROOT" \
    >>"$MONITOR_LOG" 2>&1 &
  echo $! >"$MONITOR_PID"
  echo "monitor started pid=$(cat "$MONITOR_PID")"
}

cmd_start() {
  start_sidecar
  apply_settings
  start_bulk
  start_monitor
  echo
  cmd_status
  echo
  echo "前台看进度: $0 tui"
  echo "跟日志:     $0 logs"
}

cmd_stop() {
  kill_pidfile "$BULK_PID" bulk
  kill_by_pattern 'sidecar/scripts/bulk_import_ocr.py'
  kill_pidfile "$MONITOR_PID" monitor
  kill_by_pattern 'sidecar/scripts/ocr_monitor.py'
  kill_by_pattern 'sidecar/scripts/ocr_progress_tui.py'
  if [[ "${OCR_STOP_SIDECAR:-0}" == "1" ]]; then
    kill_pidfile "$SIDECAR_PID" sidecar
    kill_by_pattern 'sidecar/main.py --host 127.0.0.1 --port 18765'
  else
    echo "sidecar kept running (set OCR_STOP_SIDECAR=1 to stop it)"
  fi
  echo "stopped"
}

cmd_restart() {
  cmd_stop
  sleep 2
  cmd_start
}

cmd_status() {
  sync_pidfile "$SIDECAR_PID" 'sidecar/main.py --host 127.0.0.1 --port 18765'
  sync_pidfile "$BULK_PID" 'sidecar/scripts/bulk_import_ocr.py'
  sync_pidfile "$MONITOR_PID" 'sidecar/scripts/ocr_monitor.py'

  echo "=== processes ==="
  for name_pid in "sidecar:$SIDECAR_PID" "bulk:$BULK_PID" "monitor:$MONITOR_PID"; do
    local name="${name_pid%%:*}" pf="${name_pid##*:}"
    if is_alive "$pf"; then
      echo "  $name RUNNING pid=$(tr -d ' \n' <"$pf")"
    else
      echo "  $name STOPPED"
    fi
  done
  pgrep -af 'sidecar/main.py|bulk_import_ocr.py|ocr_monitor.py|ocr_progress_tui.py' 2>/dev/null | grep -v 'run_bulk_ocr.sh' || true

  echo
  echo "=== health ==="
  if health_ok; then
    curl -fsS -m 3 "$BASE_URL/health"
    echo
  else
    echo "  DOWN ($BASE_URL)"
  fi

  echo
  echo "=== library docs ==="
  "$PYTHON_BIN" - <<PY
import sqlite3
from pathlib import Path
db = Path(${DATA_ROOT@Q}) / "library.db"
if not db.exists():
    print("  no library.db")
    raise SystemExit
c = sqlite3.connect(db)
rows = list(c.execute("select status, count(*) from documents group by 1 order by 1"))
total = sum(n for _, n in rows)
for s, n in rows:
    print(f"  {s:14s} {n:5d}")
print(f"  {'TOTAL':14s} {total:5d}")
jobs = list(c.execute("select status, count(*) from ocr_jobs group by 1 order by 1"))
print("=== ocr_jobs ===")
for s, n in jobs:
    print(f"  {s:14s} {n:5d}")
PY

  echo
  echo "=== bulk log (tail) ==="
  if [[ -f "$BULK_LOG" ]]; then
    tail -n 8 "$BULK_LOG" | cut -c1-160
  else
    echo "  (no log yet)"
  fi
}

cmd_tui() {
  activate_env
  exec "$PYTHON_BIN" -u sidecar/scripts/ocr_progress_tui.py --data-root "$DATA_ROOT"
}

cmd_logs() {
  touch "$BULK_LOG"
  exec tail -n 50 -F "$BULK_LOG"
}

usage() {
  sed -n '2,16p' "$0"
  echo
  echo "当前默认: DATA_ROOT=$DATA_ROOT PAGE=$PAGE DOC=$DOC API=$API WORKERS=$WORKERS"
}

main() {
  local cmd="${1:-}"
  case "$cmd" in
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    restart) cmd_restart ;;
    status)  cmd_status ;;
    tui)     cmd_tui ;;
    logs)    cmd_logs ;;
    -h|--help|help|"") usage ;;
    *)
      echo "unknown command: $cmd" >&2
      usage
      exit 1
      ;;
  esac
}

main "$@"
