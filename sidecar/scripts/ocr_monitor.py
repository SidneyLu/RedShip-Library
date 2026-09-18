#!/usr/bin/env python3
"""Monitor sidecar OCR throughput, library status, disk, and recent VL errors.

Usage:
  python ocr_monitor.py --once
  python ocr_monitor.py --interval 30
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
import urllib.request
from collections import Counter
from pathlib import Path


def fetch(url: str, timeout: float = 30):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def snapshot(base: str, data_root: Path) -> dict:
    health = fetch(f"{base}/health")
    settings = fetch(f"{base}/settings")
    try:
        metrics = fetch(f"{base}/vl-metrics?limit=200")
    except Exception:
        metrics = {}
    docs = fetch(f"{base}/library/documents?limit=100000&sort=title")
    items = list(docs.get("items") or [])
    status = Counter(str(d.get("status") or "") for d in items)
    running = [
        d
        for d in items
        if d.get("status") == "ocr_running"
        or (d.get("ocr_job") or {}).get("status") in {"running", "queued"}
    ]
    recent = list(metrics.get("items") or metrics.get("metrics") or [])
    if not recent and isinstance(metrics, list):
        recent = metrics
    ok = [m for m in recent if m.get("ok")]
    bad = [m for m in recent if m.get("ok") is False]
    durations = [float(m.get("duration_s") or 0) for m in ok if m.get("duration_s") is not None]
    avg_s = sum(durations) / len(durations) if durations else None
    # Rough RPM from recent window
    rpm = None
    if len(ok) >= 2:
        ts = sorted(float(m.get("ts") or 0) for m in ok if m.get("ts"))
        if len(ts) >= 2 and ts[-1] > ts[0]:
            rpm = len(ok) / ((ts[-1] - ts[0]) / 60.0)

    usage = shutil.disk_usage(str(data_root))
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "health": health,
        "provider": settings.get("llm_provider"),
        "concurrency": {
            "page": settings.get("ocr_page_concurrency"),
            "doc": settings.get("ocr_document_concurrency"),
            "api": settings.get("ocr_api_concurrency"),
            "workers": settings.get("ocr_worker_processes"),
            "dpi": settings.get("vision_pdf_dpi"),
        },
        "library_total": len(items),
        "status": dict(status),
        "running": len(running),
        "vl_recent_ok": len(ok),
        "vl_recent_fail": len(bad),
        "vl_avg_s": round(avg_s, 3) if avg_s is not None else None,
        "vl_est_rpm": round(rpm, 1) if rpm is not None else None,
        "disk_free_gb": round(usage.free / (1024**3), 2),
        "disk_used_pct": round(usage.used / usage.total * 100, 1),
        "fail_samples": [str(m.get("error") or "")[:120] for m in bad[-5:]],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18765")
    parser.add_argument("--data-root", default="/root/autodl-tmp/Library")
    parser.add_argument("--interval", type=float, default=0, help="Seconds; 0 = once")
    parser.add_argument("--out", default="", help="Append JSONL log path")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    data_root = Path(args.data_root)
    out = Path(args.out) if args.out else data_root / "logs" / "ocr_monitor.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    while True:
        try:
            snap = snapshot(base, data_root)
        except Exception as exc:
            snap = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "error": str(exc)}
        line = json.dumps(snap, ensure_ascii=False)
        print(line, flush=True)
        with out.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if args.interval <= 0:
            break
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
