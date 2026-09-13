"""Process and export all library documents in the Wenshi delivery format."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[2]
SIDECAR = ROOT / "sidecar"
if str(SIDECAR) not in sys.path:
    sys.path.insert(0, str(SIDECAR))

from ocr_app.db.models import Document  # noqa: E402
from ocr_app.db.session import create_tables, get_session_factory, init_engine  # noqa: E402
from ocr_app.delivery.wenshi import export_document, extract_entities, split_papers  # noqa: E402
from ocr_app.library.service import backfill_delivery_metadata  # noqa: E402
from ocr_app.settings_store import bootstrap_from_disk  # noqa: E402


async def run(args: argparse.Namespace) -> dict:
    bootstrap_from_disk()
    if args.data_root:
        os.environ["OCR_DATA_ROOT"] = str(Path(args.data_root).resolve())
        from ocr_app.config import get_settings

        get_settings.cache_clear()
    from ocr_app.config import settings

    init_engine(force=True)
    await create_tables()
    factory = get_session_factory()
    results: list[dict] = []
    async with factory() as session:
        await backfill_delivery_metadata(session)
        documents = list((await session.scalars(select(Document).order_by(Document.title))).all())
        if args.limit:
            documents = documents[: args.limit]
        for index, doc in enumerate(documents, 1):
            item: dict = {
                "document_id": doc.id,
                "title": doc.title,
                "index": index,
                "total": len(documents),
                "ok": False,
            }
            try:
                papers = await split_papers(doc, use_model=args.use_model)
                entities = await extract_entities(doc, use_model=args.use_model)
                exported = export_document(
                    doc,
                    submitter=args.submitter or settings.delivery_submitter or "未命名",
                    output_root=Path(args.output_root).resolve() if args.output_root else None,
                )
                item.update(
                    papers=len(papers),
                    unmapped=len(entities.get("_unmapped") or []),
                    export=exported,
                    ok=not exported["errors"],
                )
                if exported["errors"]:
                    item["error"] = "; ".join(exported["errors"])
            except Exception as exc:
                item["error"] = str(exc)
            results.append(item)
            print(
                f"[{index}/{len(documents)}] {'OK' if item['ok'] else 'FAIL'} {doc.title}",
                flush=True,
            )

    output_root = (
        Path(args.output_root).resolve()
        if args.output_root
        else Path(settings.data_root) / "提交"
    )
    submitter = args.submitter or settings.delivery_submitter or "未命名"
    validator = ROOT / "requirements" / "校验.py"
    validation = {"exit_code": None, "stdout": "", "stderr": ""}
    if validator.is_file():
        proc = subprocess.run(
            [sys.executable, str(validator), str(output_root / submitter)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        validation = {
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    report = {
        "data_root": str(settings.data_root),
        "output_root": str(output_root / submitter),
        "use_model": args.use_model,
        "total": len(results),
        "success_count": sum(1 for item in results if item["ok"]),
        "failure_count": sum(1 for item in results if not item["ok"]),
        "results": results,
        "validation": validation,
    }
    report_path = ROOT / ".tmp" / "wenshi_batch_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report: {report_path}")
    print(f"Success: {report['success_count']}/{report['total']}")
    print(f"Validator exit: {validation['exit_code']}")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root")
    parser.add_argument("--output-root")
    parser.add_argument("--submitter")
    parser.add_argument("--use-model", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    report = asyncio.run(run(args))
    return 0 if report["failure_count"] == 0 and report["validation"]["exit_code"] in {None, 0} else 1


if __name__ == "__main__":
    raise SystemExit(main())
