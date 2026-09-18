#!/usr/bin/env python3
"""Fix needs_rerun docs: local force-OCR contaminated pages, then Token Plan review."""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
TIBETAN_RE = re.compile(r"[\u0f00-\u0fff]")
MONGOLIAN_RE = re.compile(r"[\u1800-\u18af]")
ARABIC_RE = re.compile(r"[\u0600-\u06ff\ufb50-\ufdff]")
PAGE_RE = re.compile(r"第\s*(\d+)\s*[-–—~至到]?\s*(\d+)?\s*页|page[:\s]*(\d+)", re.I)
KW_RE = re.compile(
    r"(transformer|cuda|abstract|arxiv|neural network|dataset|python|"
    r"javascript|patient|clinical trial|http://|https://|\\\\begin\{|"
    r"LLM|ChatGPT|PyTorch|blockchain|5G|物流仓储|卷积神经网络|"
    r"叶绿体|线粒体|微信平台|高血压流行病学)",
    re.I,
)
GARBLED_RE = re.compile(r"[\$∞¥§□�]{2,}|\$~|\\\\infty")
PLACEHOLDER_RE = re.compile(
    r"(内容安全审核|网络中断|data_inspection|placeholder|未能识别|空页跳过)",
    re.I,
)
TOKEN_PLAN = {
    "llm_provider": "openai_responses",
    "openai_base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    "openai_vision_model": "qwen3.6-flash",
    "openai_chat_model": "qwen3.6-flash",
    "vision_model": "qwen3.6-flash",
    "chat_model": "qwen3.6-flash",
    "ocr_page_concurrency": 16,
    "ocr_document_concurrency": 24,
    "ocr_api_concurrency": 24,
    "ocr_worker_processes": 1,
    "vision_pdf_dpi": 200,
}
LOCAL_OCR = {
    "llm_provider": "openai_responses",
    "openai_base_url": "http://127.0.0.1:8080/v1",
    "openai_vision_model": "Qwen3.5-4B",
    "openai_chat_model": "Qwen3.5-4B",
    "ocr_page_concurrency": 6,
    "ocr_document_concurrency": 6,
    "ocr_api_concurrency": 6,
    "ocr_worker_processes": 1,
    "vision_pdf_dpi": 200,
}


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


def page_blob(page: dict) -> str:
    texts = []
    for b in page.get("blocks") or []:
        if isinstance(b, dict):
            texts.append(str(b.get("text") or ""))
    return "\n".join(texts)


def is_bad_page(blob: str) -> bool:
    if not blob.strip():
        return True
    if PLACEHOLDER_RE.search(blob) or KW_RE.search(blob) or GARBLED_RE.search(blob):
        return True
    minority = (
        len(TIBETAN_RE.findall(blob))
        + len(MONGOLIAN_RE.findall(blob))
        + len(ARABIC_RE.findall(blob))
    )
    if minority >= 20:
        return False
    ascii_n = sum(1 for ch in blob if ch.isascii() and ch.isalpha())
    cjk_n = len(CJK_RE.findall(blob))
    if ascii_n > 80 and ascii_n > cjk_n * 2:
        return True
    return False


def pages_from_issues(issues: list[str]) -> set[int]:
    out: set[int] = set()
    for iss in issues:
        for m in PAGE_RE.finditer(str(iss)):
            a, b, c = m.group(1), m.group(2), m.group(3)
            if c:
                out.add(int(c))
            elif a:
                start = int(a)
                end = int(b) if b else start
                if end < start:
                    start, end = end, start
                for p in range(start, min(end, start + 40) + 1):
                    out.add(p)
    return out


def needs_rerun_rows(db: Path) -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "select id, title, pages, review_score, review_summary from documents where status='needs_rerun'"
    ).fetchall()
    return [dict(r) for r in rows]


def build_queue(data_root: Path, rows: list[dict]) -> dict:
    docs_root = data_root / "docs"
    review_only: list[str] = []
    items: list[dict] = []
    for row in rows:
        doc_id = row["id"]
        layout_path = docs_root / doc_id / "layout.json"
        review_path = docs_root / doc_id / "review.json"
        issues: list[str] = []
        if review_path.is_file():
            try:
                rev = json.loads(review_path.read_text(encoding="utf-8"))
                issues = [str(x) for x in (rev.get("issues") or [])]
            except Exception:
                issues = []
        issue_blob = " ".join(issues) + " " + str(row.get("review_summary") or "")
        review_pipeline = any(
            x in issue_blob
            for x in (
                "data_inspection",
                "review_parse_failed",
                "review_error",
                "质检结果解析失败",
                "自动质检调用失败",
            )
        )
        bad: set[int] = set(pages_from_issues(issues))
        layout_pages = 0
        if layout_path.is_file():
            layout = json.loads(layout_path.read_text(encoding="utf-8"))
            for page in layout.get("pages") or []:
                pnum = int(page.get("page") or 0)
                layout_pages = max(layout_pages, pnum)
                if pnum >= 1 and is_bad_page(page_blob(page)):
                    bad.add(pnum)
        # Review-only: inspection/parse and no contaminated pages.
        if review_pipeline and not bad:
            review_only.append(doc_id)
            continue
        if not bad:
            # Quality fail without locatable pages: redo first/last handful.
            n = int(row.get("pages") or layout_pages or 0)
            bad.update({1, 2, 3, max(1, n // 2), max(1, n - 1), n} if n else {1})
        pages = sorted(p for p in bad if p >= 1)
        items.append(
            {
                "document_id": doc_id,
                "title": row.get("title"),
                "pages": pages,
                "page_count": len(pages),
            }
        )
    return {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ocr_docs": len(items),
        "ocr_pages": sum(x["page_count"] for x in items),
        "review_only": review_only,
        "items": items,
    }


def wait_idle(api: Api, db: Path, *, timeout_s: int, running_statuses: set[str]) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        conn = sqlite3.connect(db)
        running = int(
            conn.execute(
                f"select count(*) from documents where status in ({','.join('?' * len(running_statuses))})",
                tuple(running_statuses),
            ).fetchone()[0]
        )
        if running == 0:
            return
        time.sleep(5)
    raise TimeoutError(f"still running after {timeout_s}s: {running_statuses}")


def run_ocr_queue(api: Api, db: Path, items: list[dict], *, doc_concurrency: int) -> dict:
    progress = {"results": [], "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    in_flight: dict[str, dict] = {}
    queue = list(items)
    while queue or in_flight:
        while len(in_flight) < max(1, doc_concurrency) and queue:
            item = queue.pop(0)
            doc_id = item["document_id"]
            pages = item.get("pages") or []
            try:
                api.post(
                    f"/library/documents/{doc_id}/rerun-pages",
                    {"pages": pages, "force": True, "auto_review": False},
                    timeout=60,
                )
                in_flight[doc_id] = {"t0": time.time(), "pages": pages, "title": item.get("title")}
                log(f"ocr queued {doc_id[:8]} pages={len(pages)}")
            except Exception as exc:
                progress["results"].append({"document_id": doc_id, "ok": False, "error": str(exc)})
                log(f"ocr queue fail {doc_id[:8]}: {exc}")
        done_ids = []
        for doc_id, meta in list(in_flight.items()):
            try:
                doc = api.get(f"/library/documents/{doc_id}", timeout=30)
            except Exception:
                continue
            job = doc.get("ocr_job") or {}
            status = doc.get("status")
            running = status == "ocr_running" or job.get("status") in {"running", "queued"}
            if running:
                continue
            ok = status in {"ready", "ocr_done", "partial", "needs_rerun"}
            progress["results"].append(
                {
                    "document_id": doc_id,
                    "ok": ok,
                    "status": status,
                    "pages": meta.get("pages"),
                    "elapsed": round(time.time() - meta["t0"], 1),
                }
            )
            done_ids.append(doc_id)
            log(f"ocr done {doc_id[:8]} status={status} elapsed={time.time() - meta['t0']:.0f}s")
        for doc_id in done_ids:
            in_flight.pop(doc_id, None)
        if in_flight or queue:
            time.sleep(4)
    progress["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    ok_n = sum(1 for x in progress["results"] if x.get("ok"))
    progress["ok"] = ok_n
    progress["total"] = len(progress["results"])
    return progress


def _review_running(db: Path) -> int:
    conn = sqlite3.connect(db)
    return int(conn.execute("select count(*) from documents where status='review_running'").fetchone()[0])


def run_review(api: Api, db: Path, ids: list[str], *, batch_size: int, doc_concurrency: int) -> None:
    started = time.time()

    def enqueue(doc_ids: list[str]) -> int:
        queued_total = 0
        for i in range(0, len(doc_ids), batch_size):
            chunk = doc_ids[i : i + batch_size]
            while _review_running(db) >= doc_concurrency * 2:
                time.sleep(4)
            try:
                resp = api.post("/library/review", {"document_ids": chunk}, timeout=180)
            except Exception as exc:
                log(f"review batch fail {i}: {exc}")
                time.sleep(5)
                continue
            n = len(resp.get("queued") or [])
            queued_total += n
            log(
                f"review queued {n} skipped={len(resp.get('skipped') or [])} "
                f"batch={queued_total}/{len(doc_ids)}"
            )
        return queued_total

    enqueue(ids)
    requeued = False
    t0 = time.time()
    while time.time() - t0 < 7200:
        running = _review_running(db)
        log(f"review wait running={running}")
        if running == 0:
            if not requeued:
                stale = []
                docs_root = db.parent / "docs"
                for doc_id in ids:
                    rp = docs_root / doc_id / "review.json"
                    if not rp.is_file() or rp.stat().st_mtime < started:
                        stale.append(doc_id)
                if stale:
                    requeued = True
                    log(f"requeue stale reviews {len(stale)}")
                    enqueue(stale)
                    time.sleep(4)
                    continue
            break
        time.sleep(6)


def summarize(data_root: Path, db: Path, target_ids: list[str]) -> dict:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    all_rows = list(
        conn.execute(
            "select id, title, status, review_score, review_summary, pages from documents"
        )
    )
    by_status = Counter(r["status"] for r in all_rows)
    scores = [float(r["review_score"]) for r in all_rows if r["review_score"] is not None]
    buckets = Counter()
    for r in all_rows:
        s = r["review_score"]
        if s is None:
            buckets["unreviewed"] += 1
        elif s < 0.6:
            buckets["<0.6"] += 1
        elif s < 0.8:
            buckets["0.6-0.8"] += 1
        else:
            buckets[">=0.8"] += 1
    target = {i: None for i in target_ids}
    remaining = []
    recovered = []
    for r in all_rows:
        if r["id"] not in target:
            continue
        rec = {
            "id": r["id"],
            "title": r["title"],
            "status": r["status"],
            "score": r["review_score"],
            "summary": r["review_summary"],
        }
        if r["status"] == "needs_rerun":
            remaining.append(rec)
        else:
            recovered.append(rec)
    avg = sum(scores) / len(scores) if scores else None
    return {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "library_total": len(all_rows),
        "status": dict(by_status),
        "reviewed": len(scores),
        "avg_score": round(avg, 4) if avg is not None else None,
        "score_buckets": dict(buckets),
        "needs_rerun": by_status.get("needs_rerun", 0),
        "ready": by_status.get("ready", 0),
        "fix_targets": len(target_ids),
        "recovered": len(recovered),
        "still_needs_rerun": remaining,
        "recovered_sample": sorted(
            recovered, key=lambda x: float(x["score"] or 0)
        )[:20],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", default="/root/autodl-tmp/Library")
    parser.add_argument("--base-url", default="http://127.0.0.1:18765")
    parser.add_argument("--ocr-only", action="store_true")
    parser.add_argument("--review-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    data_root = Path(args.data_root)
    db = data_root / "library.db"
    api = Api(args.base_url)
    rows = needs_rerun_rows(db)
    log(f"needs_rerun={len(rows)}")
    queue = build_queue(data_root, rows)
    queue_path = data_root / "reports" / "fix_needs_rerun_queue.json"
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
    log(
        f"queue ocr_docs={queue['ocr_docs']} ocr_pages={queue['ocr_pages']} "
        f"review_only={len(queue['review_only'])} -> {queue_path}"
    )
    target_ids = [r["id"] for r in rows]
    if args.dry_run:
        for item in queue["items"][:15]:
            log(f"dry {item['document_id'][:8]} pages={item['page_count']} {item.get('title','')[:40]}")
        return 0

    health = api.get("/health")
    log(f"health={health}")

    ocr_progress = None
    if not args.review_only and queue["items"]:
        s = api.put("/settings", LOCAL_OCR)
        log(f"switched local url={s.get('openai_base_url')} model={s.get('openai_vision_model')}")
        try:
            ocr_progress = run_ocr_queue(api, db, queue["items"], doc_concurrency=6)
        finally:
            s = api.put("/settings", TOKEN_PLAN)
            log(f"restored token-plan url={s.get('openai_base_url')} model={s.get('openai_chat_model')}")
        ocr_path = data_root / "reports" / "fix_needs_rerun_ocr.json"
        ocr_path.write_text(json.dumps(ocr_progress, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"ocr finished ok={ocr_progress['ok']}/{ocr_progress['total']} -> {ocr_path}")

    if not args.ocr_only:
        s = api.put("/settings", TOKEN_PLAN)
        log(f"review provider url={s.get('openai_base_url')} model={s.get('openai_chat_model')}")
        run_review(api, db, target_ids, batch_size=32, doc_concurrency=24)
        summary = summarize(data_root, db, target_ids)
        out = data_root / "reports" / "review_summary.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        fix_out = data_root / "reports" / "fix_needs_rerun_summary.json"
        fix_out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        log(
            f"summary avg={summary['avg_score']} buckets={summary['score_buckets']} "
            f"needs_rerun={summary['needs_rerun']} recovered={summary['recovered']}/{summary['fix_targets']}"
        )
        log(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
