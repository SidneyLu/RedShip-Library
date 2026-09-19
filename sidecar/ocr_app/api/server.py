from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ocr_app.config import get_settings, settings
from ocr_app.db.models import Document, OcrJob, ScanJob
from ocr_app.db.session import create_tables, get_session, init_engine
from ocr_app.db.session import get_session_factory
from ocr_app.delivery.wenshi import (
    build_ocr_payload,
    export_document,
    extract_entities,
    save_papers,
    split_papers,
    validate_payload,
)
from ocr_app.jobs.runner import job_manager
from ocr_app.jobs.scan_runner import scan_job_manager
from ocr_app.library.paths import new_id
from ocr_app.library.folders import (
    delete_folder,
    ensure_folder_name,
    list_folders,
    move_documents,
    rename_folder,
)
from ocr_app.library.reindex import merge_external_docs, reindex_from_docs
from ocr_app.library.service import (
    artifact_path,
    backfill_delivery_metadata,
    delete_document,
    document_to_dict,
    get_document,
    get_document_dict,
    import_pdf,
    list_document_ids,
    list_documents,
    update_document,
)
from ocr_app.settings_store import apply_runtime_config, bootstrap_from_disk, load_persisted_settings, load_secrets
from ocr_app.ocr_core.vl_client import require_llm_configured

app = FastAPI(title="OCR Library Sidecar", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_delivery_jobs: dict[str, dict[str, Any]] = {}


class ImportBody(BaseModel):
    pdf_path: str
    title: str | None = None
    series: str | None = None
    era: str | None = None
    copy_file: bool = True
    run_ocr: bool = False
    dpi: int | None = None
    max_pages: int | None = None


class ScanBody(BaseModel):
    folder_path: str | None = None
    recursive: bool = False
    run_ocr: bool = False
    ocr_pending_in_library: bool = False
    ocr_statuses: list[str] = Field(default_factory=lambda: ["pending", "partial", "failed"])
    dpi: int | None = None
    max_pages: int | None = None
    auto_review: bool = False


class MergeDocsBody(BaseModel):
    source_path: str
    run_reindex: bool = True


class OcrBody(BaseModel):
    dpi: int | None = None
    max_pages: int | None = None
    pages: list[int] | None = None
    force: bool = False
    auto_review: bool = False


class BatchOcrBody(BaseModel):
    document_ids: list[str]
    dpi: int | None = None
    max_pages: int | None = None
    auto_review: bool = False
    force: bool = False


class BatchReviewBody(BaseModel):
    document_ids: list[str]


class PatchDocumentBody(BaseModel):
    title: str | None = None
    series: str | None = None
    era: str | None = None
    clear_series: bool = False
    delivery_metadata: dict[str, Any] | None = None


class FolderCreateBody(BaseModel):
    name: str


class FolderRenameBody(BaseModel):
    name: str


class FolderDeleteBody(BaseModel):
    move_to: str | None = None


class MoveDocumentsBody(BaseModel):
    document_ids: list[str]
    folder: str | None = None  # None / "" = uncategorized


class AbandonOcrBody(BaseModel):
    document_ids: list[str] | None = None  # None = all ocr_running
    reason: str = "OCR 任务已手动终止（僵死清理）"


class SettingsBody(BaseModel):
    data_root: str | None = None
    llm_provider: str | None = None
    dashscope_api_key: str | None = None
    dashscope_http_api_url: str | None = None
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    vision_model: str | None = None
    chat_model: str | None = None
    openai_vision_model: str | None = None
    openai_chat_model: str | None = None
    vision_pdf_dpi: int | None = None
    vision_pdf_max_pages: int | None = None
    vision_review_threshold: float | None = None
    keep_page_images: bool | None = None
    ocr_page_concurrency: int | None = None
    ocr_document_concurrency: int | None = None
    ocr_api_concurrency: int | None = None
    ocr_worker_processes: int | None = None
    delivery_submitter: str | None = None


class SavePapersBody(BaseModel):
    papers: list[dict[str, Any]]


class DeliveryProcessBody(BaseModel):
    document_ids: list[str]
    split_papers: bool = True
    extract_entities: bool = True
    use_model: bool = True
    export: bool = True
    submitter: str | None = None
    output_root: str | None = None


class DeliveryExportBody(BaseModel):
    document_ids: list[str]
    submitter: str | None = None
    output_root: str | None = None


@app.on_event("startup")
async def startup() -> None:
    bootstrap_from_disk()
    init_engine()
    await create_tables()
    async with get_session_factory()() as session:
        await backfill_delivery_metadata(session)
    from loguru import logger
    from ocr_app.jobs.ocr_worker_pool import ocr_worker_pool
    from ocr_app.ocr_core.dashscope_http_pool import install_dashscope_http_pool

    await install_dashscope_http_pool()
    try:
        await ocr_worker_pool.start()
    except Exception as exc:
        # Never block /health on worker spawn failures (common in frozen builds).
        logger.exception("OCR worker pool failed to start; falling back to in-process: {}", exc)
    # Heal DB rows left in ocr_running after a crash / restart
    abandoned = await job_manager.abandon(reason="OCR 任务在服务重启后自动清理")
    if abandoned:
        logger.warning("Startup healed {} stuck OCR document(s)", len(abandoned))


@app.on_event("shutdown")
async def shutdown() -> None:
    from ocr_app.jobs.ocr_worker_pool import ocr_worker_pool
    from ocr_app.ocr_core.dashscope_http_pool import close_dashscope_http_pool

    await ocr_worker_pool.stop()
    await close_dashscope_http_pool()


@app.post("/library/abandon-ocr")
async def api_abandon_ocr(body: AbandonOcrBody) -> dict[str, Any]:
    abandoned = await job_manager.abandon(body.document_ids, reason=body.reason)
    return {"abandoned": abandoned, "count": len(abandoned)}


@app.post("/library/documents/{doc_id}/ocr/stop")
async def api_stop_document_ocr(doc_id: str) -> dict[str, Any]:
    """Manually stop OCR for one document; keeps already-finished pages (partial)."""
    abandoned = await job_manager.abandon(
        [doc_id],
        reason="OCR 已手动停止",
    )
    if not abandoned:
        raise HTTPException(404, "document not found or OCR is not running")
    return {"document_id": doc_id, "stopped": True, "abandoned": abandoned}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "data_root": str(settings.data_root)}


@app.get("/vl-metrics")
async def vl_metrics(limit: int = 100) -> dict[str, Any]:
    """Recent vLLM / VL call timings for the status panel."""
    from ocr_app.ocr_core.openai_responses import vl_call_metrics_snapshot

    items = vl_call_metrics_snapshot(limit=max(1, min(500, int(limit or 100))))
    # Also merge recent disk log (covers worker-process writes).
    path = Path(settings.data_root) / "vl_call_metrics.jsonl"
    disk: list[dict[str, Any]] = []
    if path.is_file():
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, min(500, int(limit or 100))) :]
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    disk.append(json.loads(line))
                except Exception:
                    continue
        except Exception:
            pass
    # Prefer disk (includes worker PIDs) when present; else in-memory.
    merged = disk or items
    return {"count": len(merged), "items": merged}


@app.get("/settings")
async def get_settings_api() -> dict[str, Any]:
    persisted = load_persisted_settings()
    secrets = load_secrets()
    s = get_settings()
    provider = (s.llm_provider or "dashscope").strip().lower()
    return {
        "data_root": str(s.data_root),
        "llm_provider": provider,
        "dashscope_http_api_url": s.dashscope_http_api_url,
        "openai_base_url": s.openai_base_url,
        "vision_model": s.vision_model,
        "chat_model": s.chat_model,
        "openai_vision_model": s.openai_vision_model,
        "openai_chat_model": s.openai_chat_model,
        "vision_pdf_dpi": s.vision_pdf_dpi,
        "vision_pdf_max_pages": s.vision_pdf_max_pages,
        "vision_review_threshold": s.vision_review_threshold,
        "keep_page_images": s.keep_page_images,
        "ocr_page_concurrency": s.ocr_page_concurrency,
        "ocr_document_concurrency": s.ocr_document_concurrency,
        "ocr_api_concurrency": s.ocr_api_concurrency,
        "ocr_worker_processes": s.ocr_worker_processes,
        "delivery_submitter": s.delivery_submitter,
        "has_api_key": bool(secrets.get("dashscope_api_key") or s.dashscope_api_key),
        "has_openai_api_key": bool(secrets.get("openai_api_key") or s.openai_api_key),
        "persisted": persisted,
    }


@app.put("/settings")
async def put_settings_api(body: SettingsBody) -> dict[str, Any]:
    updates = body.model_dump(exclude_none=True)
    if "llm_provider" in updates:
        provider = str(updates["llm_provider"]).strip().lower()
        if provider not in {"dashscope", "openai_responses"}:
            raise HTTPException(400, "llm_provider must be dashscope or openai_responses")
        updates["llm_provider"] = provider
    if "ocr_worker_processes" in updates:
        try:
            updates["ocr_worker_processes"] = max(1, min(16, int(updates["ocr_worker_processes"])))
        except (TypeError, ValueError):
            updates["ocr_worker_processes"] = 1

    before = get_settings()
    prev_api = int(before.ocr_api_concurrency)
    prev_workers = int(before.ocr_worker_processes)

    apply_runtime_config(updates)
    init_engine(force="data_root" in updates)
    await create_tables()

    # Persist first; pool rebuild must not block or fail the save response.
    need_pool = False
    if "ocr_api_concurrency" in updates and int(get_settings().ocr_api_concurrency) != prev_api:
        need_pool = True
    if "ocr_worker_processes" in updates and int(get_settings().ocr_worker_processes) != prev_workers:
        need_pool = True
    if {
        "llm_provider",
        "dashscope_api_key",
        "dashscope_http_api_url",
        "openai_api_key",
        "openai_base_url",
        "vision_model",
        "chat_model",
        "openai_vision_model",
        "openai_chat_model",
    }.intersection(updates):
        need_pool = True
    if need_pool:
        try:
            from ocr_app.jobs.ocr_worker_pool import ocr_worker_pool
            from ocr_app.ocr_core.dashscope_http_pool import install_dashscope_http_pool
            from loguru import logger

            await ocr_worker_pool.reconfigure()
            await install_dashscope_http_pool(
                force=True,
                worker_count=max(1, int(get_settings().ocr_worker_processes)),
            )
        except Exception as exc:
            from loguru import logger

            logger.exception("Settings saved but OCR pool reconfigure failed: {}", exc)
    return await get_settings_api()


@app.get("/library/documents")
async def api_list_documents(
    session: AsyncSession = Depends(get_session),
    q: str | None = None,
    status: str | None = None,
    series: str | None = None,
    uncategorized: bool = False,
    sort: str = "updated_at",
    limit: int = 20000,
    offset: int = 0,
) -> dict[str, Any]:
    items, total = await list_documents(
        session,
        q=q,
        status=status,
        series=series,
        uncategorized=uncategorized,
        sort=sort,
        limit=max(1, min(int(limit), 100000)),
        offset=max(0, int(offset)),
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/library/documents/ids")
async def api_list_document_ids(
    session: AsyncSession = Depends(get_session),
    q: str | None = None,
    status: str | None = None,
    series: str | None = None,
    uncategorized: bool = False,
    sort: str = "updated_at",
) -> dict[str, Any]:
    ids, total = await list_document_ids(
        session,
        q=q,
        status=status,
        series=series,
        uncategorized=uncategorized,
        sort=sort,
    )
    return {"ids": ids, "total": total}


@app.get("/library/folders")
async def api_list_folders(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    return await list_folders(session)


@app.post("/library/folders")
async def api_create_folder(body: FolderCreateBody) -> dict[str, Any]:
    try:
        name = ensure_folder_name(body.name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"name": name}


@app.patch("/library/folders/{name}")
async def api_rename_folder(
    name: str, body: FolderRenameBody, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    try:
        return await rename_folder(session, name, body.name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.delete("/library/folders/{name}")
async def api_delete_folder(
    name: str,
    session: AsyncSession = Depends(get_session),
    move_to: str | None = None,
) -> dict[str, Any]:
    try:
        return await delete_folder(session, name, move_to=move_to)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/library/move-documents")
async def api_move_documents(
    body: MoveDocumentsBody, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    try:
        return await move_documents(session, body.document_ids, folder=body.folder)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/library/documents/{doc_id}")
async def api_get_document(doc_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    data = await get_document_dict(session, doc_id)
    if not data:
        raise HTTPException(404, "document not found")
    return data


@app.post("/library/import")
async def api_import(body: ImportBody, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    pdf = Path(body.pdf_path)
    doc = await import_pdf(
        session,
        pdf_path=pdf,
        title=body.title,
        series=body.series,
        era=body.era,
        copy=body.copy_file,
    )
    result: dict[str, Any] = {"document": document_to_dict(doc)}
    if body.run_ocr:
        if job_manager.is_document_running(doc.id):
            from ocr_app.library.service import latest_ocr_job

            existing_job = await latest_ocr_job(session, doc.id)
            result["job"] = {
                "job_id": existing_job.id if existing_job else None,
                "document_id": doc.id,
                "already_running": True,
            }
        else:
            # Heal stale DB flag left after crash / abandon race
            if doc.status == "ocr_running":
                doc.status = "partial" if doc.block_count > 0 else "pending"
                doc.error = None
                await session.commit()
                await session.refresh(doc)
                result["document"] = document_to_dict(doc)
            try:
                result["job"] = await _start_ocr_job(
                    session, doc.id, dpi=body.dpi, max_pages=body.max_pages, pages=None
                )
            except HTTPException as exc:
                if exc.status_code == 409:
                    result["ocr_skipped"] = "already_running"
                else:
                    raise
    return result


async def _queue_ocr_jobs(
    session: AsyncSession,
    doc_ids: list[str],
    *,
    dpi: int | None,
    max_pages: int | None,
    auto_review: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Start OCR for doc_ids; skip running/already-queued conflicts."""
    jobs: list[dict[str, Any]] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for doc_id in doc_ids:
        if doc_id in seen:
            continue
        seen.add(doc_id)
        if job_manager.is_document_running(doc_id):
            skipped.append(doc_id)
            continue
        try:
            job_info = await _start_ocr_job(
                session,
                doc_id,
                dpi=dpi,
                max_pages=max_pages,
                pages=None,
                auto_review=auto_review,
            )
            jobs.append(job_info)
        except HTTPException as exc:
            if exc.status_code == 409:
                skipped.append(doc_id)
            else:
                raise
    return jobs, skipped


async def _start_ocr_job(
    session: AsyncSession,
    doc_id: str,
    *,
    dpi: int | None,
    max_pages: int | None,
    pages: list[int] | None,
    force: bool = False,
    auto_review: bool = False,
) -> dict[str, Any]:
    cfg_err = require_llm_configured()
    if cfg_err:
        raise HTTPException(400, cfg_err)
    if not job_manager.try_claim(doc_id):
        raise HTTPException(409, "OCR already running for this document")
    job_id = new_id()
    try:
        job = OcrJob(
            id=job_id,
            document_id=doc_id,
            status="running",
            current_page=0,
            total_pages=0,
            pages_requested=json.dumps(pages or []),
            started_at=datetime.now(timezone.utc),
        )
        session.add(job)
        await session.commit()
        task = asyncio.create_task(
            job_manager.run_ocr(
                document_id=doc_id,
                job_id=job_id,
                dpi=dpi,
                max_pages=max_pages,
                pages=pages,
                force=force,
                auto_review=auto_review,
            )
        )
        job_manager.track_task(doc_id, task)
        return {"job_id": job_id, "document_id": doc_id}
    except Exception:
        job_manager.release(doc_id)
        raise


async def _start_review_job(session: AsyncSession, doc_id: str) -> dict[str, Any]:
    cfg_err = require_llm_configured()
    if cfg_err:
        raise HTTPException(400, cfg_err)
    from ocr_app.library.paths import doc_dir

    layout_path = doc_dir(doc_id) / "layout.json"
    if not layout_path.is_file():
        raise HTTPException(400, "layout.json not found; run OCR first")
    if not job_manager.try_claim(doc_id):
        raise HTTPException(409, "OCR/review already running for this document")
    job_id = new_id()
    try:
        job = OcrJob(
            id=job_id,
            document_id=doc_id,
            status="running",
            current_page=0,
            total_pages=1,
            pages_requested=json.dumps([]),
            started_at=datetime.now(timezone.utc),
        )
        session.add(job)
        await session.commit()
        task = asyncio.create_task(
            job_manager.run_review(document_id=doc_id, job_id=job_id)
        )
        job_manager.track_task(doc_id, task)
        return {"job_id": job_id, "document_id": doc_id}
    except Exception:
        job_manager.release(doc_id)
        raise


async def _queue_review_jobs(
    session: AsyncSession,
    doc_ids: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    jobs: list[dict[str, Any]] = []
    skipped: list[str] = []
    seen: set[str] = set()
    from ocr_app.library.paths import doc_dir

    for doc_id in doc_ids:
        if doc_id in seen:
            continue
        seen.add(doc_id)
        if job_manager.is_document_running(doc_id):
            skipped.append(doc_id)
            continue
        if not (doc_dir(doc_id) / "layout.json").is_file():
            skipped.append(doc_id)
            continue
        try:
            jobs.append(await _start_review_job(session, doc_id))
        except HTTPException as exc:
            if exc.status_code in {400, 409}:
                skipped.append(doc_id)
            else:
                raise
    return jobs, skipped


@app.post("/library/scan")
async def api_scan(body: ScanBody, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    folder = Path(body.folder_path or settings.data_root)
    if not folder.is_dir():
        raise HTTPException(400, f"folder not found: {folder}")

    scan_job_id = new_id()
    options = body.model_dump()
    job = ScanJob(
        id=scan_job_id,
        status="running",
        folder_path=str(folder),
        current=0,
        total=0,
        options_json=json.dumps(options, ensure_ascii=False),
        started_at=datetime.now(timezone.utc),
    )
    session.add(job)
    await session.commit()

    asyncio.create_task(
        scan_job_manager.run_scan(
            scan_job_id=scan_job_id,
            folder=folder,
            recursive=body.recursive,
            run_ocr=body.run_ocr,
            ocr_pending_in_library=body.ocr_pending_in_library,
            ocr_statuses=body.ocr_statuses,
            dpi=body.dpi,
            max_pages=body.max_pages,
            queue_ocr_fn=_queue_ocr_jobs,
            auto_review=body.auto_review,
        )
    )

    return {"scan_job_id": scan_job_id, "status": "running"}


@app.post("/library/reindex")
async def api_reindex(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    report = await reindex_from_docs(session)
    return {
        "added": report.added,
        "updated": report.updated,
        "skipped": report.skipped,
        "skipped_duplicate_sha": report.skipped_duplicate_sha,
        "invalid": report.invalid,
        "added_count": len(report.added),
        "updated_count": len(report.updated),
    }


@app.post("/library/merge-docs")
async def api_merge_docs(
    body: MergeDocsBody, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    source = Path(body.source_path)
    if not source.is_dir():
        raise HTTPException(400, f"source path not found: {source}")
    report = await merge_external_docs(session, source, run_reindex=body.run_reindex)
    out: dict[str, Any] = {
        "copied": report.copied,
        "skipped_same": report.skipped_same,
        "conflicts": report.conflicts,
        "invalid": report.invalid,
        "copied_count": len(report.copied),
    }
    if report.reindex:
        r = report.reindex
        out["reindex"] = {
            "added": r.added,
            "updated": r.updated,
            "skipped_duplicate_sha": r.skipped_duplicate_sha,
            "invalid": r.invalid,
            "added_count": len(r.added),
            "updated_count": len(r.updated),
        }
    return out


@app.patch("/library/documents/{doc_id}")
async def api_patch_document(
    doc_id: str, body: PatchDocumentBody, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    try:
        doc = await update_document(
            session,
            doc_id,
            title=body.title,
            series=body.series,
            era=body.era,
            clear_series=body.clear_series,
            delivery_metadata=body.delivery_metadata,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not doc:
        raise HTTPException(404, "document not found")
    return document_to_dict(doc)


@app.delete("/library/documents/{doc_id}")
async def api_delete_document(doc_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, bool]:
    ok = await delete_document(session, doc_id)
    if not ok:
        raise HTTPException(404, "document not found")
    return {"ok": True}


@app.get("/library/documents/{doc_id}/thumb")
async def api_thumb(doc_id: str) -> FileResponse:
    p = artifact_path(doc_id, "thumb.png")
    if not p:
        raise HTTPException(404, "thumb not found")
    return FileResponse(
        p,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/library/documents/{doc_id}/source.pdf")
async def api_source_pdf(doc_id: str) -> FileResponse:
    p = artifact_path(doc_id, "source.pdf")
    if not p:
        raise HTTPException(404, "pdf not found")
    return FileResponse(p, media_type="application/pdf")


@app.get("/library/documents/{doc_id}/artifacts/{name}")
async def api_artifact(doc_id: str, name: str) -> FileResponse:
    allowed = {"layout.json", "content.md", "review.json", "papers.json", "entities.json"}
    if name not in allowed:
        raise HTTPException(400, "invalid artifact")
    p = artifact_path(doc_id, name)
    if not p:
        raise HTTPException(404, f"{name} not found")
    media = "application/json" if name.endswith(".json") else "text/markdown"
    return FileResponse(p, media_type=media)


async def _run_delivery_job(job_id: str, body: DeliveryProcessBody) -> None:
    state = _delivery_jobs[job_id]
    results: list[dict[str, Any]] = []
    factory = get_session_factory()
    try:
        for index, doc_id in enumerate(body.document_ids, 1):
            state.update(current=index - 1, current_document_id=doc_id)
            result: dict[str, Any] = {"document_id": doc_id, "ok": False}
            try:
                async with factory() as session:
                    doc = await session.get(Document, doc_id)
                    if not doc:
                        raise ValueError("document not found")
                    if body.split_papers:
                        result["papers"] = len(
                            await split_papers(doc, use_model=body.use_model)
                        )
                    if body.extract_entities:
                        entity_data = await extract_entities(
                            doc, use_model=body.use_model
                        )
                        result["unmapped"] = len(entity_data.get("_unmapped") or [])
                    if body.export:
                        exported = export_document(
                            doc,
                            submitter=body.submitter or settings.delivery_submitter or "未命名",
                            output_root=Path(body.output_root) if body.output_root else None,
                        )
                        result["export"] = exported
                        if exported["errors"]:
                            raise ValueError("; ".join(exported["errors"]))
                    result["ok"] = True
            except Exception as exc:
                result["error"] = str(exc)[:1000]
            results.append(result)
            state.update(current=index, results=results)
        state["status"] = "completed"
    except Exception as exc:
        state.update(status="failed", error=str(exc)[:2000])
    finally:
        state["finished_at"] = datetime.now(timezone.utc).isoformat()
        report = {
            **state,
            "success_count": sum(1 for item in results if item.get("ok")),
            "failure_count": sum(1 for item in results if not item.get("ok")),
        }
        report_path = Path(settings.data_root) / "wenshi_batch_report.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        state["report_path"] = str(report_path)


@app.get("/library/documents/{doc_id}/delivery")
async def api_get_delivery(
    doc_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    doc = await get_document(session, doc_id)
    if not doc:
        raise HTTPException(404, "document not found")
    ddir = Path(settings.data_root) / "docs" / doc_id
    papers = json.loads((ddir / "papers.json").read_text(encoding="utf-8")) if (ddir / "papers.json").is_file() else []
    entities = json.loads((ddir / "entities.json").read_text(encoding="utf-8")) if (ddir / "entities.json").is_file() else None
    return {
        "document": document_to_dict(doc),
        "papers": papers,
        "entities": entities,
        "ocr_validation": dict(zip(("errors", "warnings"), validate_payload(build_ocr_payload(doc)))),
    }


@app.put("/library/documents/{doc_id}/papers")
async def api_save_papers(
    doc_id: str,
    body: SavePapersBody,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    doc = await get_document(session, doc_id)
    if not doc:
        raise HTTPException(404, "document not found")
    try:
        papers = save_papers(doc_id, body.papers, doc.pages)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"document_id": doc_id, "papers": papers}


@app.post("/library/process/wenshi")
async def api_process_wenshi(body: DeliveryProcessBody) -> dict[str, Any]:
    if not body.document_ids:
        raise HTTPException(400, "document_ids required")
    job_id = new_id()
    _delivery_jobs[job_id] = {
        "id": job_id,
        "status": "running",
        "current": 0,
        "total": len(body.document_ids),
        "results": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    asyncio.create_task(_run_delivery_job(job_id, body))
    return {"job_id": job_id, "total": len(body.document_ids)}


@app.get("/delivery-jobs/{job_id}")
async def api_get_delivery_job(job_id: str) -> dict[str, Any]:
    job = _delivery_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "delivery job not found")
    return job


@app.post("/library/export/wenshi")
async def api_export_wenshi(
    body: DeliveryExportBody,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if not body.document_ids:
        raise HTTPException(400, "document_ids required")
    results: list[dict[str, Any]] = []
    for doc_id in body.document_ids:
        doc = await get_document(session, doc_id)
        if not doc:
            results.append({"document_id": doc_id, "errors": ["document not found"]})
            continue
        try:
            results.append(
                export_document(
                    doc,
                    submitter=body.submitter or settings.delivery_submitter or "未命名",
                    output_root=Path(body.output_root) if body.output_root else None,
                )
            )
        except Exception as exc:
            results.append({"document_id": doc_id, "errors": [str(exc)]})
    return {
        "results": results,
        "success_count": sum(1 for result in results if not result.get("errors")),
        "failure_count": sum(1 for result in results if result.get("errors")),
    }


@app.post("/library/validate/wenshi")
async def api_validate_wenshi(
    body: DeliveryExportBody,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for doc_id in body.document_ids:
        doc = await get_document(session, doc_id)
        if not doc:
            results.append({"document_id": doc_id, "errors": ["document not found"], "warnings": []})
            continue
        try:
            errors, warnings = validate_payload(build_ocr_payload(doc, submitter=body.submitter))
            results.append({"document_id": doc_id, "errors": errors, "warnings": warnings})
        except Exception as exc:
            results.append({"document_id": doc_id, "errors": [str(exc)], "warnings": []})
    return {"results": results}


@app.post("/library/documents/{doc_id}/ocr")
async def api_run_ocr(
    doc_id: str, body: OcrBody, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    doc = await get_document(session, doc_id)
    if not doc:
        raise HTTPException(404, "document not found")
    return await _start_ocr_job(
        session,
        doc_id,
        dpi=body.dpi,
        max_pages=body.max_pages,
        pages=body.pages,
        force=body.force,
        auto_review=body.auto_review,
    )


@app.post("/library/documents/{doc_id}/rerun-pages")
async def api_rerun_pages(
    doc_id: str, body: OcrBody, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    if not body.pages:
        raise HTTPException(400, "pages required")
    doc = await get_document(session, doc_id)
    if not doc:
        raise HTTPException(404, "document not found")
    # Explicit page rerun always overwrites existing page results.
    return await _start_ocr_job(
        session,
        doc_id,
        dpi=body.dpi,
        max_pages=body.max_pages,
        pages=body.pages,
        force=True,
        auto_review=body.auto_review,
    )


@app.post("/library/documents/{doc_id}/review")
async def api_run_review(
    doc_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    doc = await get_document(session, doc_id)
    if not doc:
        raise HTTPException(404, "document not found")
    return await _start_review_job(session, doc_id)


@app.post("/library/ocr")
async def api_batch_ocr(
    body: BatchOcrBody, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    if not body.document_ids:
        raise HTTPException(400, "document_ids required")
    jobs, skipped = await _queue_ocr_jobs(
        session,
        body.document_ids,
        dpi=body.dpi,
        max_pages=body.max_pages,
        auto_review=body.auto_review,
    )
    return {
        "jobs": jobs,
        "queued": [j["document_id"] for j in jobs],
        "skipped": skipped,
        "count": len(jobs),
    }


@app.post("/library/review")
async def api_batch_review(
    body: BatchReviewBody, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    if not body.document_ids:
        raise HTTPException(400, "document_ids required")
    jobs, skipped = await _queue_review_jobs(session, body.document_ids)
    return {
        "jobs": jobs,
        "queued": [j["document_id"] for j in jobs],
        "skipped": skipped,
        "count": len(jobs),
    }


@app.get("/jobs/{job_id}")
async def api_get_job(job_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    job = await session.get(OcrJob, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return {
        "id": job.id,
        "document_id": job.document_id,
        "status": job.status,
        "current_page": job.current_page,
        "total_pages": job.total_pages,
        "pages_requested": json.loads(job.pages_requested or "[]"),
        "error": job.error,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


@app.get("/jobs/{job_id}/events")
async def api_job_events(job_id: str) -> StreamingResponse:
    q = job_manager.subscribe(job_id)

    async def gen():
        try:
            while True:
                event = await q.get()
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("type") in {"done", "error"}:
                    break
        finally:
            job_manager.unsubscribe(job_id)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/scan-jobs/{job_id}")
async def api_get_scan_job(job_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    job = await session.get(ScanJob, job_id)
    if not job:
        raise HTTPException(404, "scan job not found")
    result = json.loads(job.result_json) if job.result_json else None
    return {
        "id": job.id,
        "status": job.status,
        "current": job.current,
        "total": job.total,
        "folder_path": job.folder_path,
        "error": job.error,
        "result": result,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


@app.get("/scan-jobs/{job_id}/events")
async def api_scan_job_events(job_id: str) -> StreamingResponse:
    q = scan_job_manager.subscribe(job_id)

    async def gen():
        try:
            while True:
                event = await q.get()
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("type") in {"done", "error"}:
                    break
        finally:
            scan_job_manager.unsubscribe(job_id)

    return StreamingResponse(gen(), media_type="text/event-stream")
