#!/usr/bin/env python3
"""Export ready documents to the Wenshi 提交 directory (heuristic papers + TIME entities)."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

SIDECAR = Path(__file__).resolve().parents[1]
if str(SIDECAR) not in sys.path:
    sys.path.insert(0, str(SIDECAR))

SUBMITTER_DEFAULT = "未命名"


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def _init_worker(data_root: str) -> None:
    os.environ["OCR_DATA_ROOT"] = data_root
    if str(SIDECAR) not in sys.path:
        sys.path.insert(0, str(SIDECAR))
    from ocr_app.settings_store import bootstrap_from_disk

    bootstrap_from_disk()
    from ocr_app.config import get_settings

    get_settings.cache_clear()
    from ocr_app.db.session import init_engine

    init_engine(force=True)


def _export_one(doc_id: str, submitter: str, output_root: str) -> dict:
    async def _run() -> dict:
        from ocr_app.db.models import Document
        from ocr_app.db.session import get_session_factory
        from ocr_app.delivery.wenshi import export_document, extract_entities, split_papers

        factory = get_session_factory()
        async with factory() as session:
            doc = await session.get(Document, doc_id)
            if not doc:
                return {"document_id": doc_id, "ok": False, "error": "document not found"}
            title = doc.title
            await split_papers(doc, use_model=False)
            await extract_entities(doc, use_model=False)
            exported = export_document(
                doc,
                submitter=submitter,
                output_root=Path(output_root),
            )
            return {
                "document_id": doc_id,
                "title": title,
                "ok": not exported.get("errors"),
                "ocr_path": exported.get("ocr_path"),
                "entity_path": exported.get("entity_path"),
                "errors": exported.get("errors") or [],
                "warnings": exported.get("warnings") or [],
            }

    try:
        return asyncio.run(_run())
    except Exception as exc:
        return {"document_id": doc_id, "ok": False, "error": str(exc)[:500]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", default="/root/autodl-tmp/Library")
    parser.add_argument("--submitter", default=SUBMITTER_DEFAULT)
    parser.add_argument("--output-root", default="")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    data_root = Path(args.data_root).resolve()
    output_root = Path(args.output_root).resolve() if args.output_root else data_root / "提交"
    db = data_root / "library.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    ready = list(
        conn.execute(
            "select id, title, pages, review_score from documents where status='ready' order by title"
        )
    )
    skipped = list(
        conn.execute(
            "select id, title, pages, review_score, review_summary from documents where status!='ready' order by title"
        )
    )
    if args.limit:
        ready = ready[: args.limit]
    ids = [r["id"] for r in ready]
    log(f"export ready={len(ids)} skipped={len(skipped)} workers={args.workers} out={output_root}")

    results: list[dict] = []
    ok_n = fail_n = 0
    t0 = time.time()
    with ProcessPoolExecutor(
        max_workers=max(1, args.workers),
        initializer=_init_worker,
        initargs=(str(data_root),),
    ) as pool:
        futs = {
            pool.submit(_export_one, doc_id, args.submitter, str(output_root)): doc_id
            for doc_id in ids
        }
        for i, fut in enumerate(as_completed(futs), 1):
            item = fut.result()
            results.append(item)
            if item.get("ok"):
                ok_n += 1
            else:
                fail_n += 1
                log(f"FAIL {item.get('document_id','')[:8]} {item.get('error') or item.get('errors')}")
            if i % 100 == 0 or i == len(ids):
                rate = i / max(time.time() - t0, 0.001)
                log(f"progress {i}/{len(ids)} ok={ok_n} fail={fail_n} {rate:.1f}/s")

    report = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "data_root": str(data_root),
        "output_root": str(output_root / args.submitter),
        "submitter": args.submitter,
        "ready_total": len(ids),
        "success_count": ok_n,
        "failure_count": fail_n,
        "skipped_not_ready": [
            {
                "id": r["id"],
                "title": r["title"],
                "status": "needs_rerun",
                "score": r["review_score"],
                "pages": r["pages"],
                "summary": r["review_summary"],
            }
            for r in skipped
        ],
        "failures": [x for x in results if not x.get("ok")],
        "elapsed_s": round(time.time() - t0, 1),
    }
    report_path = data_root / "reports" / "export_ready_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    catalog = [
        {
            "id": x.get("document_id"),
            "title": x.get("title"),
            "ok": x.get("ok"),
            "ocr_path": x.get("ocr_path"),
            "entity_path": x.get("entity_path"),
        }
        for x in results
    ]
    catalog_path = output_root / args.submitter / "catalog.json"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"done ok={ok_n}/{len(ids)} fail={fail_n} report={report_path}")
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
