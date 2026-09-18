#!/usr/bin/env python3
"""Short quota probe: enqueue N docs and report pages/min + VL latency / failures.

Helps decide whether to ramp ocr_api_concurrency toward RPM 10000 / TPM 10M.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class Api:
    def __init__(self, base: str, timeout: float = 300) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout

    def req(self, method: str, path: str, body=None):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} {method} {path}: {detail}") from exc

    def get(self, path: str):
        return self.req("GET", path)

    def post(self, path: str, body=None):
        return self.req("POST", path, body)

    def put(self, path: str, body=None):
        return self.req("PUT", path, body)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18765")
    parser.add_argument("--docs", type=int, default=4, help="How many pending docs to probe")
    parser.add_argument("--page", type=int, default=64)
    parser.add_argument("--doc", type=int, default=4)
    parser.add_argument("--api", type=int, default=256)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--duration", type=int, default=180, help="Observe seconds after queue")
    args = parser.parse_args()

    api = Api(args.base_url)
    health = api.get("/health")
    settings = api.get("/settings")
    log(f"health={health} provider={settings.get('llm_provider')} key={settings.get('has_api_key')}")
    if not settings.get("has_api_key"):
        log("ERROR: no API key")
        return 2

    api.put(
        "/settings",
        {
            "ocr_page_concurrency": args.page,
            "ocr_document_concurrency": args.doc,
            "ocr_api_concurrency": args.api,
            "ocr_worker_processes": args.workers,
            "vision_pdf_dpi": 300,
            "llm_provider": "dashscope",
        },
    )
    log(f"set concurrency page={args.page} doc={args.doc} api={args.api} workers={args.workers}")

    docs = api.get("/library/documents?limit=100000&sort=title").get("items") or []
    pending = [
        d["id"]
        for d in docs
        if d.get("status") in {"pending", "partial", "failed", "needs_rerun"}
    ][: max(1, args.docs)]
    if not pending:
        log("no pending docs to probe")
        return 1

    t0 = time.time()
    resp = api.post("/library/ocr", {"document_ids": pending, "force": False, "auto_review": False})
    log(f"queued jobs={len(resp.get('jobs') or [])} skipped={len(resp.get('skipped') or [])}")

    pages_before = sum(int(d.get("pages") or 0) for d in docs if d["id"] in set(pending))
    # Observe
    deadline = time.time() + max(30, args.duration)
    last_blocks = 0
    while time.time() < deadline:
        time.sleep(15)
        cur = api.get("/library/documents?limit=100000&sort=title").get("items") or []
        by_id = {d["id"]: d for d in cur}
        running = 0
        blocks = 0
        done = 0
        for doc_id in pending:
            d = by_id.get(doc_id) or {}
            job = d.get("ocr_job") or {}
            if d.get("status") == "ocr_running" or job.get("status") in {"running", "queued"}:
                running += 1
            if d.get("status") in {"ready", "ocr_done", "partial", "needs_rerun"}:
                done += 1
            blocks += int(d.get("block_count") or 0)
        elapsed = max(1.0, time.time() - t0)
        delta_blocks = blocks - last_blocks
        last_blocks = blocks
        log(
            f"t={elapsed:.0f}s running={running}/{len(pending)} done={done} "
            f"blocks={blocks} (+{delta_blocks})"
        )

    elapsed = max(1.0, time.time() - t0)
    # Prefer metrics if available
    try:
        metrics = api.get("/vl-metrics?limit=500")
        recent = list(metrics.get("items") or metrics.get("metrics") or [])
        if isinstance(metrics, list):
            recent = metrics
    except Exception:
        recent = []
    # Only count metrics from this probe window (ignore historical jsonl noise).
    recent = [m for m in recent if float(m.get("ts") or 0) >= t0 - 5]
    ok = [m for m in recent if m.get("ok")]
    bad = [m for m in recent if m.get("ok") is False]
    durs = [float(m.get("duration_s") or 0) for m in ok if m.get("duration_s")]
    avg = sum(durs) / len(durs) if durs else None
    rpm = None
    if len(ok) >= 2:
        ts = sorted(float(m.get("ts") or 0) for m in ok if m.get("ts"))
        if len(ts) >= 2 and ts[-1] > ts[0]:
            rpm = len(ok) / ((ts[-1] - ts[0]) / 60.0)

    report = {
        "elapsed_s": round(elapsed, 1),
        "docs": len(pending),
        "concurrency": {"page": args.page, "doc": args.doc, "api": args.api, "workers": args.workers},
        "vl_ok": len(ok),
        "vl_fail": len(bad),
        "vl_avg_s": round(avg, 3) if avg else None,
        "est_rpm": round(rpm, 1) if rpm else None,
        "fail_samples": [str(m.get("error") or "")[:160] for m in bad[-8:]],
        "hint": (
            "ramp api +128/+256 if est_rpm << 9000 and fail_samples lack 429; "
            "stop rising if TPM/429 dominate"
        ),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
