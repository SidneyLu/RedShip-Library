#!/usr/bin/env python3
"""Batch-run /library/review via sidecar and write a summary JSON."""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.error
import urllib.request
from collections import Counter
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
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} {method} {path}: {detail}") from exc

    def get(self, path: str):
        return self.req("GET", path)

    def post(self, path: str, body=None, timeout: float | None = None):
        return self.req("POST", path, body, timeout=timeout)

    def put(self, path: str, body=None):
        return self.req("PUT", path, body)


def pending_ids(db: Path) -> list[str]:
    c = sqlite3.connect(db)
    rows = c.execute(
        """
        select id from documents
        where status in ('ocr_done', 'partial')
          and (review_score is null or status = 'ocr_done')
        order by id
        """
    ).fetchall()
    return [r[0] for r in rows]


def status_snapshot(db: Path) -> dict:
    c = sqlite3.connect(db)
    docs = dict(c.execute("select status, count(*) from documents group by 1"))
    running = int(
        c.execute("select count(*) from documents where status='review_running'").fetchone()[0]
    )
    scored = int(
        c.execute("select count(*) from documents where review_score is not null").fetchone()[0]
    )
    return {"docs": docs, "review_running": running, "scored": scored}


def summarize(data_root: Path, db: Path) -> dict:
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    rows = list(
        c.execute(
            "select id, title, status, review_score, review_summary, pages, block_count from documents"
        )
    )
    scores = [float(r["review_score"]) for r in rows if r["review_score"] is not None]
    by_status = Counter(r["status"] for r in rows)
    buckets = Counter()
    parse_fail = 0
    low = []
    for r in rows:
        s = r["review_score"]
        if s is None:
            buckets["unreviewed"] += 1
            continue
        if s < 0.6:
            buckets["<0.6"] += 1
            low.append(
                {
                    "id": r["id"],
                    "title": r["title"],
                    "score": s,
                    "status": r["status"],
                    "summary": r["review_summary"],
                }
            )
        elif s < 0.8:
            buckets["0.6-0.8"] += 1
        else:
            buckets[">=0.8"] += 1
        summary = str(r["review_summary"] or "")
        if "解析失败" in summary or "质检调用失败" in summary:
            parse_fail += 1
    issues: Counter[str] = Counter()
    docs_root = data_root / "docs"
    for ddir in docs_root.iterdir() if docs_root.is_dir() else []:
        rp = ddir / "review.json"
        if not rp.is_file():
            continue
        try:
            rev = json.loads(rp.read_text(encoding="utf-8"))
        except Exception:
            continue
        for iss in rev.get("issues") or []:
            issues[str(iss)[:80]] += 1
    avg = sum(scores) / len(scores) if scores else None
    return {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "library_total": len(rows),
        "status": dict(by_status),
        "reviewed": len(scores),
        "unreviewed": buckets.get("unreviewed", 0),
        "avg_score": round(avg, 4) if avg is not None else None,
        "score_buckets": dict(buckets),
        "needs_rerun": by_status.get("needs_rerun", 0),
        "ready": by_status.get("ready", 0),
        "parse_or_call_fail": parse_fail,
        "top_issues": issues.most_common(20),
        "lowest": sorted(low, key=lambda x: x["score"])[:30],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", default="/root/autodl-tmp/Library")
    parser.add_argument("--base-url", default="http://127.0.0.1:18765")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--poll-s", type=float, default=8.0)
    parser.add_argument("--doc-concurrency", type=int, default=24)
    parser.add_argument("--api-concurrency", type=int, default=24)
    args = parser.parse_args()

    data_root = Path(args.data_root)
    db = data_root / "library.db"
    api = Api(args.base_url)
    health = api.get("/health")
    settings = api.get("/settings")
    log(
        f"health={health} provider={settings.get('llm_provider')} "
        f"chat={settings.get('openai_chat_model') or settings.get('chat_model')} "
        f"url={settings.get('openai_base_url')}"
    )

    ids = pending_ids(db)
    log(f"pending review={len(ids)}")
    queued_total = 0
    skipped_total = 0
    for i in range(0, len(ids), args.batch_size):
        chunk = ids[i : i + args.batch_size]
        # Keep the in-flight window from exploding.
        while True:
            snap = status_snapshot(db)
            running = snap["review_running"]
            if running < args.doc_concurrency * 2:
                break
            time.sleep(args.poll_s)
        try:
            resp = api.post("/library/review", {"document_ids": chunk}, timeout=180)
        except Exception as exc:
            log(f"batch fail {i}: {exc}")
            time.sleep(5)
            continue
        n = len(resp.get("queued") or [])
        skipped_total += len(resp.get("skipped") or [])
        queued_total += n
        log(
            f"queued {n} skipped={len(resp.get('skipped') or [])} "
            f"total_queued={queued_total}/{len(ids)} running={status_snapshot(db)['review_running']}"
        )

    while True:
        snap = status_snapshot(db)
        left = pending_ids(db)
        log(
            f"wait remaining={len(left)} running={snap['review_running']} "
            f"scored={snap['scored']} status={snap['docs']}"
        )
        if not left and snap["review_running"] == 0:
            break
        # Re-queue leftovers that never started.
        if left and snap["review_running"] == 0:
            log(f"requeue leftovers {len(left)}")
            for i in range(0, len(left), args.batch_size):
                try:
                    api.post("/library/review", {"document_ids": left[i : i + args.batch_size]})
                except Exception as exc:
                    log(f"requeue fail: {exc}")
            time.sleep(args.poll_s)
            continue
        time.sleep(args.poll_s)

    summary = summarize(data_root, db)
    out = data_root / "reports" / "review_summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"summary -> {out}")
    log(
        f"reviewed={summary['reviewed']} avg={summary['avg_score']} "
        f"buckets={summary['score_buckets']} needs_rerun={summary['needs_rerun']} "
        f"ready={summary['ready']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
