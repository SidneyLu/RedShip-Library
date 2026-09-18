#!/usr/bin/env python3
"""Rerun OCR for placeholder pages after switching sidecar to OpenAI Responses (local).

Typical flow:
  1. Start llama-server / vLLM with OpenAI Responses-compatible /v1
  2. PUT settings: llm_provider=openai_responses, openai_base_url=...
  3. python scan_inspection_pages.py
  4. python rerun_inspection_pages.py
  5. Switch back to dashscope when done
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class Api:
    def __init__(self, base: str, timeout: float = 300) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout

    def req(self, method: str, path: str, body=None, timeout: float | None = None):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} {method} {path}: {detail}") from exc

    def get(self, path: str, timeout: float | None = None):
        return self.req("GET", path, timeout=timeout)

    def post(self, path: str, body=None, timeout: float | None = None):
        return self.req("POST", path, body, timeout=timeout)

    def put(self, path: str, body=None, timeout: float | None = None):
        return self.req("PUT", path, body, timeout=timeout)


def switch_to_openai(api: Api, base_url: str, vision: str, chat: str) -> dict:
    body = {
        "llm_provider": "openai_responses",
        "openai_base_url": base_url.rstrip("/"),
        "openai_vision_model": vision,
        "openai_chat_model": chat or vision,
    }
    return api.put("/settings", body)


def switch_to_dashscope(api: Api) -> dict:
    return api.put("/settings", {"llm_provider": "dashscope"})


def wait_doc(api: Api, doc_id: str, *, timeout_s: int = 3600) -> dict:
    t0 = time.time()
    seen = False
    while time.time() - t0 < timeout_s:
        doc = api.get(f"/library/documents/{doc_id}")
        job = doc.get("ocr_job") or {}
        status = doc.get("status")
        running = status == "ocr_running" or job.get("status") in {"running", "queued"}
        if running:
            seen = True
        elif seen or status in {"ready", "ocr_done", "partial", "needs_rerun", "failed"}:
            return doc
        time.sleep(5)
    return api.get(f"/library/documents/{doc_id}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", default="/root/autodl-tmp/Library")
    parser.add_argument("--base-url", default="http://127.0.0.1:18765")
    parser.add_argument("--queue", default="", help="Queue JSON from scan_inspection_pages.py")
    parser.add_argument("--openai-base-url", default="http://127.0.0.1:8080/v1")
    parser.add_argument("--vision-model", default="Qwen3.5-4B")
    parser.add_argument("--chat-model", default="Qwen3.5-4B")
    parser.add_argument("--doc-concurrency", type=int, default=2)
    parser.add_argument("--api-concurrency", type=int, default=1)
    parser.add_argument("--page-concurrency", type=int, default=2)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--switch-openai", action="store_true", help="Switch provider before rerun")
    parser.add_argument("--switch-back", action="store_true", help="Switch back to dashscope after")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    data_root = Path(args.data_root)
    queue_path = Path(args.queue) if args.queue else data_root / "reports" / "rerun_inspection_queue.json"
    if not queue_path.is_file():
        queue_path = data_root / "rerun_inspection_queue.json"
    if not queue_path.is_file():
        log(f"missing queue {queue_path}; run scan_inspection_pages.py first")
        return 2
    payload = json.loads(queue_path.read_text(encoding="utf-8"))
    items = list(payload.get("items") or [])
    log(f"queue docs={len(items)} pages={payload.get('page_count')}")

    api = Api(args.base_url)
    health = api.get("/health")
    settings = api.get("/settings")
    log(f"health={health.get('ok')} provider={settings.get('llm_provider')}")

    if args.switch_openai:
        s = switch_to_openai(api, args.openai_base_url, args.vision_model, args.chat_model)
        log(f"switched provider={s.get('llm_provider')} url={s.get('openai_base_url')}")

    api.put(
        "/settings",
        {
            "ocr_page_concurrency": args.page_concurrency,
            "ocr_document_concurrency": args.doc_concurrency,
            "ocr_api_concurrency": args.api_concurrency,
            "ocr_worker_processes": args.workers,
            "vision_pdf_dpi": args.dpi,
        },
    )

    progress_path = data_root / "reports" / "rerun_inspection_progress.json"
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    progress = {"results": [], "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if args.dry_run:
        for item in items[:20]:
            log(f"dry-run {item['document_id'][:8]} pages={item.get('pages')}")
        log(f"dry-run total docs={len(items)}")
        return 0

    in_flight: dict[str, dict] = {}
    queue = list(items)
    while queue or in_flight:
        while len(in_flight) < max(1, args.doc_concurrency) and queue:
            item = queue.pop(0)
            doc_id = item["document_id"]
            pages = item.get("pages") or []
            try:
                if pages:
                    api.post(
                        f"/library/documents/{doc_id}/rerun-pages",
                        {"pages": pages, "force": True, "auto_review": False},
                    )
                else:
                    # markdown_only or unknown pages: full non-force OCR reprocesses placeholders
                    api.post(
                        f"/library/documents/{doc_id}/ocr",
                        {"force": False, "auto_review": False},
                    )
                in_flight[doc_id] = {"t0": time.time(), "pages": pages}
                log(f"queued {doc_id[:8]} pages={pages or 'placeholders'}")
            except Exception as exc:
                progress["results"].append(
                    {"document_id": doc_id, "ok": False, "error": str(exc)}
                )
                log(f"queue fail {doc_id[:8]}: {exc}")

        done_ids = []
        for doc_id, meta in list(in_flight.items()):
            doc = api.get(f"/library/documents/{doc_id}")
            job = doc.get("ocr_job") or {}
            status = doc.get("status")
            running = status == "ocr_running" or job.get("status") in {"running", "queued"}
            if running:
                continue
            progress["results"].append(
                {
                    "document_id": doc_id,
                    "ok": status in {"ready", "ocr_done", "partial", "needs_rerun"},
                    "status": status,
                    "pages": meta.get("pages"),
                    "elapsed": time.time() - meta["t0"],
                }
            )
            done_ids.append(doc_id)
            log(f"done {doc_id[:8]} status={status}")
        for doc_id in done_ids:
            in_flight.pop(doc_id, None)

        progress["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        progress_path.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
        if in_flight or queue:
            time.sleep(5)

    if args.switch_back:
        s = switch_to_dashscope(api)
        log(f"switched back provider={s.get('llm_provider')}")

    ok_n = sum(1 for x in progress["results"] if x.get("ok"))
    log(f"rerun finished ok={ok_n}/{len(progress['results'])} -> {progress_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
