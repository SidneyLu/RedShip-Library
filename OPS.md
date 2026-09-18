# RedShip-Library 冲配额运行说明（本机 AutoDL）

## 已就绪

- Library: `/root/autodl-tmp/Library`（~6039 册，`secrets.json` 已含 DashScope Key）
- Sidecar: `127.0.0.1:18765`，`OCR_DATA_ROOT` 指向上述目录
- 并发夹紧已放开（Settings UI：api≤2048 / page≤256 / doc≤64 / workers≤16）
- 当前运行配置：DPI=300，workers=8，page=128，doc=32，api=512（可再升到 ~800–1200）
- 已修：tenacity+loguru 在 DashScope 错误含 `{}` 时的 `KeyError: '"code"'`（重试日志不再炸）
- 主跑：`bulk_import_ocr.py` 多文档并行（非 doc=1）+ `ocr_monitor.py` 后台监控

## 云端主跑

推荐一键批处理（启停 / 状态 / TUI / 日志）：

```bash
cd /root/autodl-tmp/RedShip-Library
./scripts/run_bulk_ocr.sh start      # sidecar + bulk + monitor
./scripts/run_bulk_ocr.sh status
./scripts/run_bulk_ocr.sh tui        # 前台进度
./scripts/run_bulk_ocr.sh logs
./scripts/run_bulk_ocr.sh stop       # 停 bulk/monitor，默认保留 sidecar
./scripts/run_bulk_ocr.sh restart

# 调并发（重启生效）
OCR_PAGE=64 OCR_DOC=32 OCR_API=96 OCR_WORKERS=8 ./scripts/run_bulk_ocr.sh restart
# 连 sidecar 一起停
OCR_STOP_SIDECAR=1 ./scripts/run_bulk_ocr.sh stop
```

手动等价命令：

```bash
# sidecar（若未起）
/root/autodl-tmp/RedShip-Library/scripts/start_sidecar.sh

# 多文档 OCR（已去掉 doc=1；自动 ramp）
conda activate OCR
cd /root/autodl-tmp/RedShip-Library
python sidecar/scripts/bulk_import_ocr.py \
  --data-root /root/autodl-tmp/Library --ocr-only --skip-probe \
  --page-concurrency 64 --doc-concurrency 32 --api-concurrency 96 --workers 8

# 短探针
python sidecar/scripts/quota_probe.py --docs 4 --api 256 --duration 180

# 监控
python sidecar/scripts/ocr_monitor.py --interval 60
```

日志：`Library/bulk_ocr.log`、`Library/bulk_ocr_progress.json`、`Library/ocr_monitor.jsonl`  
PID：`Library/run/{sidecar,bulk,monitor}.pid`

目标：DashScope **RPM 10000 / TPM 10M**。无 429 时按探针抬高 `ocr_api_concurrency`；TPM 先顶满则停加并发。

## 审核页本地重跑（阶段二）

```bash
# 1) 扫占位页
python sidecar/scripts/scan_inspection_pages.py --data-root /root/autodl-tmp/Library

# 2) 起本地 Responses 兼容服务（需 llama-server）
export LLAMA_SERVER=/root/autodl-tmp/llama.cpp/build/bin/llama-server
/root/autodl-tmp/RedShip-Library/scripts/start_llama_server.sh

# 3) 一键切 OpenAI + 重跑
python sidecar/scripts/rerun_inspection_pages.py \
  --switch-openai --openai-base-url http://127.0.0.1:8080/v1 \
  --vision-model Qwen3.5-4B --switch-back
```

设置页也可「一键切到 OpenAI Responses / 切回 DashScope」。
