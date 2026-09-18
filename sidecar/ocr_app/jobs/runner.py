from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from ocr_app.config import active_vision_model, settings
from ocr_app.db.models import Document, OcrJob
from ocr_app.library.paths import abs_from_relative, doc_dir
from ocr_app.library.service import (
    get_document,
    mark_document_ocr_done,
    mark_document_ready,
    mark_document_running,
)
from ocr_app.ocr_core.dashscope_vl import (
    _is_transient_disconnect,
    is_data_inspection_error,
)
from ocr_app.ocr_core.vl_client import get_vl_client
from ocr_app.ocr_core.vision_pdf import (
    LayoutBlock,
    blocks_to_layout,
    blocks_to_markdown,
    layout_blocks_from_dict,
    load_layout_file,
    merge_page_blocks,
    pages_to_process,
    pages_with_content,
    parse_layout_json_text,
    render_pdf_page,
    render_pdf_pages,
    render_thumbnail,
    write_artifacts,
)
from ocr_app.ocr_core.vision_review import review_vision_markdown

# Extra emphasis only on rare second-pass; first call already includes STRICT_BBOX_HINT.
STRICT_HINT = (
    "\n再次强调：每个文字块必须有独立、紧贴文字的 bbox。"
    "禁止输出整页 [0,0,1000,1000]。标题与正文必须分开。"
)

# Keep in sync with PLACEHOLDER_TEXT_MARKERS in vision_pdf.py
SKIPPED_CONN_NOTE = "【本页因网络中断未能识别，可在工作台对该页单独重跑】"
SKIPPED_INSPECTION_NOTE = "【本页因云端内容安全审核未能识别，可在工作台对该页单独重跑】"
SKIPPED_EMPTY_NOTE = "【本页模型返回空结果未能识别，可在工作台对该页单独重跑】"


def _is_empty_content_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "empty content" in msg or "returned empty" in msg

PageDoneCallback = Callable[[int, list[LayoutBlock]], Awaitable[None]]
EmitCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


def _blocks_need_strict_retry(page_blocks: list[LayoutBlock]) -> bool:
    bad = sum(
        1
        for b in page_blocks
        if (b.bbox[2] - b.bbox[0]) * (b.bbox[3] - b.bbox[1]) >= 0.85 * 1e6
    )
    return bad >= max(1, len(page_blocks) // 2)


def _pages_total(doc_pages: int, layout: dict[str, Any], blocks: list[LayoutBlock]) -> int:
    return max(
        doc_pages,
        len(layout.get("pages") or []),
        max((b.page for b in blocks), default=0),
    )


async def _resolve_page_image(
    *,
    pdf_path: Path,
    tmp_path: Path,
    img_map: dict[int, Path],
    page_num: int,
    dpi_val: int,
) -> Path:
    img = img_map.get(page_num)
    if img and img.is_file():
        return img
    single_dir = tmp_path / f"p{page_num}"
    out = single_dir / "page.png"
    await asyncio.to_thread(render_pdf_page, pdf_path, page_num, out, dpi=dpi_val)
    img_map[page_num] = out
    return out


async def _ocr_page_blocks(img: Path, page_num: int, *, extra_hint: str | None = None) -> list[LayoutBlock]:
    """OCR one page. DataInspectionFailed → placeholder skip (do not fail the whole job)."""
    from ocr_app.ocr_core.vision_pdf import is_nearly_blank_image

    # Blank / near-blank pages make thinking VL models hang without useful output.
    if await asyncio.to_thread(is_nearly_blank_image, img):
        logger.info("Page {}: nearly blank image — skip VL call", page_num)
        try:
            from ocr_app.ocr_core.openai_responses import _record_vl_metric
            import time as _time

            _record_vl_metric(
                {
                    "ts": _time.time(),
                    "kind": "blank_skip",
                    "page": page_num,
                    "image": img.name,
                    "duration_s": 0.0,
                    "ok": True,
                    "error": None,
                }
            )
        except Exception:
            pass
        return []

    try:
        raw = await get_vl_client().extract_page_layout(
            img, page=page_num, extra_hint=extra_hint
        )
        page_blocks = parse_layout_json_text(raw, page_num, repair=True)
        if _blocks_need_strict_retry(page_blocks):
            try:
                raw2 = await get_vl_client().extract_page_layout(
                    img, page=page_num, extra_hint=(extra_hint or "") + STRICT_HINT
                )
                page_blocks = parse_layout_json_text(raw2, page_num, repair=True)
            except Exception as exc:
                if is_data_inspection_error(exc):
                    # Second pass blocked; keep first-pass blocks (already repaired).
                    logger.warning(
                        "Page {}: DataInspectionFailed on strict retry; keeping first pass",
                        page_num,
                    )
                    return page_blocks
                raise
        return page_blocks
    except Exception as exc:
        if is_data_inspection_error(exc):
            logger.warning(
                "Page {}: DataInspectionFailed — skipping page ({})",
                page_num,
                exc,
            )
            return _skipped_inspection_blocks(page_num)
        if _is_empty_content_error(exc):
            logger.warning(
                "Page {}: empty VL content after retries — skipping page ({})",
                page_num,
                exc,
            )
            return _skipped_empty_blocks(page_num)
        raise


def _skipped_connection_blocks(page_num: int) -> list[LayoutBlock]:
    return [
        LayoutBlock(
            type="text",
            text=SKIPPED_CONN_NOTE,
            bbox=[60.0, 60.0, 940.0, 120.0],
            page=page_num,
        )
    ]


def _skipped_inspection_blocks(page_num: int) -> list[LayoutBlock]:
    return [
        LayoutBlock(
            type="text",
            text=SKIPPED_INSPECTION_NOTE,
            bbox=[60.0, 60.0, 940.0, 120.0],
            page=page_num,
        )
    ]


def _skipped_empty_blocks(page_num: int) -> list[LayoutBlock]:
    return [
        LayoutBlock(
            type="text",
            text=SKIPPED_EMPTY_NOTE,
            bbox=[60.0, 60.0, 940.0, 120.0],
            page=page_num,
        )
    ]


async def _ocr_single_page(
    page_num: int,
    *,
    pdf_path: Path,
    tmp_path: Path,
    img_map: dict[int, Path],
    dpi_val: int,
    page_sem: asyncio.Semaphore,
) -> tuple[int, list[LayoutBlock]]:
    async with page_sem:
        img = await _resolve_page_image(
            pdf_path=pdf_path,
            tmp_path=tmp_path,
            img_map=img_map,
            page_num=page_num,
            dpi_val=dpi_val,
        )
        # Tenacity already retries inside the client; keep only a light page-level
        # outer loop so we don't stampede after gateway disconnects.
        last_err: BaseException | None = None
        for attempt in range(2):
            try:
                return page_num, await _ocr_page_blocks(img, page_num)
            except Exception as exc:
                if not _is_transient_disconnect(exc):
                    raise
                last_err = exc
                wait_s = 8 * (attempt + 1)
                logger.warning(
                    "Page {}: server disconnected ({}); page retry {}/2 in {}s",
                    page_num,
                    exc,
                    attempt + 1,
                    wait_s,
                )
                from ocr_app.ocr_core.vl_rate_limiter import vl_limiter

                await vl_limiter.on_transient_failure()
                await asyncio.sleep(wait_s)
        logger.error("Page {}: giving up after disconnects: {}", page_num, last_err)
        return page_num, _skipped_connection_blocks(page_num)


async def _process_pages_via_worker_pool(
    *,
    pages_list: list[int],
    pdf_path: Path,
    dpi_val: int,
    document_id: str,
    job: OcrJob,
    job_id: str,
    session,
    on_page_done: PageDoneCallback | None = None,
    initial_done: int = 0,
    document_total: int = 0,
    emit: EmitCallback,
) -> dict[int, list[LayoutBlock]]:
    from ocr_app.jobs.ocr_worker_pool import (
        cancel_page_jobs_for_job,
        enqueue_page_jobs,
        ocr_worker_pool,
        wait_for_page_jobs,
    )

    if not ocr_worker_pool.active:
        raise RuntimeError("OCR worker pool is not running")

    doc_total = document_total or len(pages_list)
    await enqueue_page_jobs(
        document_id=document_id,
        job_id=job_id,
        pages=pages_list,
        pdf_path=pdf_path,
        dpi=dpi_val,
        keep_page_images=bool(settings.keep_page_images),
    )

    completed = 0
    last_commit_at = 0.0
    page_results: dict[int, list[LayoutBlock]] = {}

    async def _on_page(page_num: int, page_blocks: list[LayoutBlock]) -> None:
        nonlocal completed, last_commit_at
        page_results[page_num] = page_blocks
        if on_page_done:
            await on_page_done(page_num, page_blocks)
        completed += 1
        job.current_page = initial_done + completed
        job.total_pages = doc_total
        now = time.monotonic()
        if completed >= len(pages_list) or now - last_commit_at >= 1.5:
            await session.commit()
            last_commit_at = now
        await emit(
            job_id,
            {
                "type": "progress",
                "current_page": initial_done + completed,
                "total_pages": doc_total,
                "page": page_num,
                "batch_current": completed,
                "batch_total": len(pages_list),
            },
        )

    async def _should_abort() -> bool:
        task = asyncio.current_task()
        if task is None:
            return False
        cancelling = getattr(task, "cancelling", None)
        if callable(cancelling):
            return cancelling() > 0
        return False

    try:
        await wait_for_page_jobs(
            job_id=job_id,
            pages=pages_list,
            document_id=document_id,
            on_page_done=_on_page,
            should_abort=_should_abort,
        )
    except (Exception, asyncio.CancelledError):
        ocr_worker_pool.request_cancel(document_id)
        await cancel_page_jobs_for_job(job_id)
        raise
    finally:
        ocr_worker_pool.clear_cancel(document_id)

    return page_results


async def _finalize_document(
    *,
    session,
    doc,
    job: OcrJob,
    job_id: str,
    ddir: Path,
    layout_state: dict[str, Any],
    pages_total: int,
    pdf_path: Path,
    emit: EmitCallback,
    auto_review: bool = False,
) -> None:
    all_blocks = layout_blocks_from_dict(layout_state)
    layout = blocks_to_layout(all_blocks, pages=int(pages_total) or 1)
    markdown = blocks_to_markdown(all_blocks, title=doc.title)

    empty_pages = 0
    present = pages_with_content(layout)
    for p in range(1, int(pages_total) + 1):
        if p not in present:
            empty_pages += 1

    review: dict[str, Any] | None = None
    if auto_review:
        review = await review_vision_markdown(
            markdown,
            pages=int(pages_total),
            block_count=len(all_blocks),
            empty_pages=empty_pages,
        )
        if SKIPPED_CONN_NOTE in markdown or SKIPPED_INSPECTION_NOTE in markdown:
            review["needs_rerun"] = True
            summary = str(review.get("summary") or "").strip()
            notes: list[str] = []
            if SKIPPED_CONN_NOTE in markdown:
                notes.append("部分页面因网络中断被跳过")
            if SKIPPED_INSPECTION_NOTE in markdown:
                notes.append("部分页面因内容安全审核被跳过")
            note = "；".join(notes)
            review["summary"] = f"{summary}；{note}" if summary else note

    write_artifacts(ddir, markdown=markdown, layout=layout, review=review)

    try:
        await asyncio.to_thread(render_thumbnail, pdf_path, ddir / "thumb.png")
    except Exception:
        pass

    job.status = "done"
    job.finished_at = datetime.now(timezone.utc)
    await session.commit()

    if auto_review and review is not None:
        await mark_document_ready(
            session,
            doc,
            pages=int(pages_total),
            block_count=len(all_blocks),
            review_score=float(review.get("score", 0)),
            review_summary=str(review.get("summary") or ""),
            vision_model=active_vision_model(),
            needs_rerun=bool(review.get("needs_rerun")),
        )
        await emit(job_id, {"type": "done", "review": review})
    else:
        await mark_document_ocr_done(
            session,
            doc,
            pages=int(pages_total),
            block_count=len(all_blocks),
            vision_model=active_vision_model(),
        )
        await emit(job_id, {"type": "done", "review": None, "status": "ocr_done"})


class JobManager:
    def __init__(self) -> None:
        self._events: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._running: set[str] = set()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # Dynamic document slots: always honor live settings.ocr_document_concurrency
        # (unlike a fixed Semaphore, which cannot shrink after settings change).
        self._doc_slot_lock = asyncio.Lock()
        self._doc_slot_cond = asyncio.Condition(self._doc_slot_lock)
        self._doc_slots_active = 0

    def is_document_running(self, document_id: str) -> bool:
        return document_id in self._running

    def active_document_ids(self) -> set[str]:
        return set(self._running)

    def try_claim(self, document_id: str) -> bool:
        """Atomically mark document as OCR-running. Returns False if already claimed."""
        if document_id in self._running:
            return False
        self._running.add(document_id)
        return True

    def release(self, document_id: str) -> None:
        self._running.discard(document_id)
        self._tasks.pop(document_id, None)

    @asynccontextmanager
    async def _document_semaphore(self):
        """Gate concurrent documents using the current settings limit."""
        while True:
            async with self._doc_slot_lock:
                limit = max(1, int(settings.ocr_document_concurrency))
                if self._doc_slots_active < limit:
                    self._doc_slots_active += 1
                    break
            async with self._doc_slot_cond:
                await self._doc_slot_cond.wait()
        try:
            yield
        finally:
            async with self._doc_slot_lock:
                self._doc_slots_active = max(0, self._doc_slots_active - 1)
            async with self._doc_slot_cond:
                self._doc_slot_cond.notify_all()

    def subscribe(self, job_id: str) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._events[job_id] = q
        return q

    def unsubscribe(self, job_id: str) -> None:
        self._events.pop(job_id, None)

    async def _emit(self, job_id: str, event: dict[str, Any]) -> None:
        q = self._events.get(job_id)
        if q:
            await q.put(event)

    def track_task(self, document_id: str, task: asyncio.Task[None]) -> None:
        self._tasks[document_id] = task

        def _cleanup(t: asyncio.Task[None]) -> None:
            if self._tasks.get(document_id) is t:
                self._tasks.pop(document_id, None)

        task.add_done_callback(_cleanup)

    async def _mark_abandoned(
        self,
        document_id: str,
        job_id: str | None,
        reason: str,
        *,
        running_status: str = "ocr_running",
    ) -> None:
        from ocr_app.db.session import get_session_factory
        from ocr_app.library.service import get_document

        factory = get_session_factory()
        async with factory() as session:
            doc = await get_document(session, document_id)
            job = await session.get(OcrJob, job_id) if job_id else None
            if job is None and doc is not None:
                # Fall back to latest running job for this document
                from sqlalchemy import select

                result = await session.execute(
                    select(OcrJob)
                    .where(OcrJob.document_id == document_id, OcrJob.status == "running")
                    .order_by(OcrJob.started_at.desc())
                    .limit(1)
                )
                job = result.scalar_one_or_none()
            if job and job.status == "running":
                job.status = "failed"
                job.error = reason[:2000]
                job.finished_at = datetime.now(timezone.utc)
            if doc and doc.status == running_status:
                if running_status == "review_running":
                    doc.error = reason[:2000]
                    doc.status = "ocr_done"
                else:
                    layout_path = doc_dir(document_id) / "layout.json"
                    if doc.block_count == 0 and layout_path.is_file():
                        doc.block_count = len(
                            layout_blocks_from_dict(load_layout_file(layout_path))
                        )
                    doc.error = reason[:2000]
                    doc.status = "partial" if doc.block_count > 0 else "failed"
            await session.commit()
        if job_id:
            await self._emit(job_id, {"type": "error", "error": reason})

    async def abandon(
        self,
        document_ids: list[str] | None = None,
        *,
        reason: str = "OCR 任务已手动终止（僵死清理）",
    ) -> list[dict[str, Any]]:
        """Cancel in-flight OCR/review and/or heal DB rows stuck in running states."""
        from ocr_app.db.session import get_session_factory
        from sqlalchemy import select

        factory = get_session_factory()
        async with factory() as session:
            q = select(Document).where(
                Document.status.in_(["ocr_running", "review_running"])
            )
            if document_ids:
                q = q.where(Document.id.in_(document_ids))
            rows = list((await session.execute(q)).scalars().all())
            targets = [(d.id, d.status) for d in rows]

        abandoned: list[dict[str, Any]] = []
        for doc_id, doc_status in targets:
            from ocr_app.jobs.ocr_worker_pool import (
                cancel_page_jobs_for_document,
                ocr_worker_pool,
            )

            ocr_worker_pool.request_cancel(doc_id)
            await cancel_page_jobs_for_document(doc_id)
            task = self._tasks.get(doc_id)
            job_id: str | None = None
            # Prefer cancelling live task; it will mark DB in CancelledError handler
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            else:
                await self._mark_abandoned(
                    doc_id,
                    job_id,
                    reason,
                    running_status=str(doc_status or "ocr_running"),
                )
            self._running.discard(doc_id)
            self._tasks.pop(doc_id, None)
            ocr_worker_pool.clear_cancel(doc_id)
            abandoned.append({"document_id": doc_id, "cancelled_live": bool(task)})
        return abandoned

    async def _process_pages_parallel(
        self,
        *,
        pages_list: list[int],
        pdf_path: Path,
        tmp_path: Path,
        img_map: dict[int, Path],
        dpi_val: int,
        job: OcrJob,
        job_id: str,
        session,
        on_page_done: PageDoneCallback | None = None,
        initial_done: int = 0,
        document_total: int = 0,
    ) -> dict[int, list[LayoutBlock]]:
        page_concurrency = max(1, int(settings.ocr_page_concurrency))
        page_sem = asyncio.Semaphore(page_concurrency)
        doc_total = document_total or len(pages_list)

        tasks = [
            asyncio.create_task(
                _ocr_single_page(
                    page_num,
                    pdf_path=pdf_path,
                    tmp_path=tmp_path,
                    img_map=img_map,
                    dpi_val=dpi_val,
                    page_sem=page_sem,
                ),
                name=f"ocr-page-{page_num}",
            )
            for page_num in pages_list
        ]

        page_results: dict[int, list[LayoutBlock]] = {}
        completed = 0
        last_commit_at = 0.0
        try:
            for coro in asyncio.as_completed(tasks):
                page_num, page_blocks = await coro
                page_results[page_num] = page_blocks
                if on_page_done:
                    await on_page_done(page_num, page_blocks)
                completed += 1
                job.current_page = initial_done + completed
                job.total_pages = doc_total
                # Throttle DB writes: every ~1.5s or on last page (avoids SQLite lock storms)
                now = time.monotonic()
                if completed >= len(pages_list) or now - last_commit_at >= 1.5:
                    await session.commit()
                    last_commit_at = now
                await self._emit(
                    job_id,
                    {
                        "type": "progress",
                        "current_page": initial_done + completed,
                        "total_pages": doc_total,
                        "page": page_num,
                        "batch_current": completed,
                        "batch_total": len(pages_list),
                    },
                )
        except (Exception, asyncio.CancelledError):
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return page_results

    async def run_ocr(
        self,
        *,
        document_id: str,
        job_id: str,
        dpi: int | None = None,
        max_pages: int | None = None,
        pages: list[int] | None = None,
        force: bool = False,
        auto_review: bool = False,
    ) -> None:
        # Prefer claim in _start_ocr_job; keep defensive claim for direct callers.
        if document_id not in self._running:
            self._running.add(document_id)
        from ocr_app.db.session import get_session_factory

        factory = get_session_factory()
        dpi_val = max(72, dpi or settings.vision_pdf_dpi)
        max_p = max(1, max_pages or settings.vision_pdf_max_pages)
        explicit_pages = pages

        try:
            async with self._document_semaphore():
                async with factory() as session:
                    doc = await get_document(session, document_id)
                    if not doc:
                        raise FileNotFoundError(f"document {document_id}")
                    pdf_path = abs_from_relative(doc.relative_path)
                    if not pdf_path.is_file():
                        raise FileNotFoundError(str(pdf_path))

                    job = await session.get(OcrJob, job_id)
                    if not job:
                        raise FileNotFoundError(f"job {job_id}")

                    await mark_document_running(session, doc, dpi_val)
                    job.status = "running"
                    job.current_page = 0

                    from ocr_app.ocr_core.vision_pdf import pdf_page_count

                    doc_page_count = doc.pages or pdf_page_count(pdf_path)
                    target_total = min(doc_page_count, max_p)
                    target_pages = list(range(1, target_total + 1))

                    ddir = doc_dir(document_id)
                    layout_path = ddir / "layout.json"
                    # Preserve existing layout except for a forced full-document redo.
                    layout_state: dict[str, Any] = (
                        {}
                        if force and explicit_pages is None
                        else load_layout_file(layout_path)
                    )

                    pages_list = pages_to_process(
                        target_pages,
                        layout_state,
                        explicit_pages=explicit_pages,
                        force=force,
                    )

                    requested_count = (
                        len(explicit_pages) if explicit_pages is not None else len(target_pages)
                    )
                    skipped = max(0, requested_count - len(pages_list))
                    document_total = doc_page_count
                    if skipped > 0:
                        await self._emit(
                            job_id,
                            {
                                "type": "resume",
                                "skipped_pages": skipped,
                                "remaining": len(pages_list),
                                "total_pages": document_total,
                            },
                        )
                        logger.info(
                            "Resuming OCR for {}: skip {} done pages, {} remaining",
                            document_id,
                            skipped,
                            len(pages_list),
                        )

                    job.total_pages = document_total
                    job.current_page = skipped
                    job.pages_requested = json.dumps(pages_list or target_pages)
                    await session.commit()

                    checkpoint_lock = asyncio.Lock()
                    checkpoint_pages = 0

                    async def save_checkpoint(page_num: int, page_blocks: list[LayoutBlock]) -> None:
                        nonlocal layout_state, checkpoint_pages
                        async with checkpoint_lock:
                            layout_state = merge_page_blocks(
                                layout_state, page_num, page_blocks, force=force
                            )
                            blocks = layout_blocks_from_dict(layout_state)
                            markdown = blocks_to_markdown(blocks, title=doc.title)
                            write_artifacts(
                                ddir,
                                markdown=markdown,
                                layout=layout_state,
                                review=None,
                            )
                            doc.block_count = len(blocks)
                            checkpoint_pages += 1
                            # Disk checkpoint every page; DB every 5 pages
                            if checkpoint_pages % 5 == 0:
                                await session.commit()

                    if not pages_list:
                        pages_total = _pages_total(
                            doc_page_count,
                            layout_state,
                            layout_blocks_from_dict(layout_state),
                        )
                        await _finalize_document(
                            session=session,
                            doc=doc,
                            job=job,
                            job_id=job_id,
                            ddir=ddir,
                            layout_state=layout_state,
                            pages_total=pages_total,
                            pdf_path=pdf_path,
                            emit=self._emit,
                            auto_review=auto_review,
                        )
                        return

                    from ocr_app.jobs.ocr_worker_pool import (
                        ocr_worker_pool,
                        worker_processes_enabled,
                    )

                    if worker_processes_enabled() and ocr_worker_pool.active:
                        await _process_pages_via_worker_pool(
                            pages_list=pages_list,
                            pdf_path=pdf_path,
                            dpi_val=dpi_val,
                            document_id=document_id,
                            job=job,
                            job_id=job_id,
                            session=session,
                            on_page_done=save_checkpoint,
                            initial_done=skipped,
                            document_total=document_total,
                            emit=self._emit,
                        )
                    else:
                        with tempfile.TemporaryDirectory(prefix="ocr_pages_") as tmp:
                            tmp_path = Path(tmp)
                            max_render = max(pages_list) if pages_list else target_total
                            page_images = await asyncio.to_thread(
                                render_pdf_pages,
                                pdf_path,
                                tmp_path,
                                dpi=dpi_val,
                                max_pages=max_render,
                            )
                            img_map = {i + 1: p for i, p in enumerate(page_images)}

                            await self._process_pages_parallel(
                                pages_list=pages_list,
                                pdf_path=pdf_path,
                                tmp_path=tmp_path,
                                img_map=img_map,
                                dpi_val=dpi_val,
                                job=job,
                                job_id=job_id,
                                session=session,
                                on_page_done=save_checkpoint,
                                initial_done=skipped,
                                document_total=document_total,
                            )

                            if settings.keep_page_images:
                                pages_dir = ddir / "pages"
                                pages_dir.mkdir(parents=True, exist_ok=True)
                                for pnum, img in img_map.items():
                                    if img.is_file():
                                        shutil.copy2(
                                            img, pages_dir / f"page-{pnum:04d}.png"
                                        )

                    pages_total = _pages_total(
                        doc_page_count,
                        layout_state,
                        layout_blocks_from_dict(layout_state),
                    )
                    await _finalize_document(
                        session=session,
                        doc=doc,
                        job=job,
                        job_id=job_id,
                        ddir=ddir,
                        layout_state=layout_state,
                        pages_total=pages_total,
                        pdf_path=pdf_path,
                        emit=self._emit,
                        auto_review=auto_review,
                    )

        except asyncio.CancelledError:
            logger.warning("OCR job cancelled for {}", document_id)
            await self._mark_abandoned(
                document_id,
                job_id,
                "OCR 已手动停止",
            )
            raise
        except Exception as e:
            logger.exception("OCR job failed: {}", e)
            async with factory() as session:
                doc = await get_document(session, document_id)
                job = await session.get(OcrJob, job_id)
                if job:
                    job.status = "failed"
                    job.error = str(e)[:2000]
                    job.finished_at = datetime.now(timezone.utc)
                if doc:
                    layout_path = doc_dir(document_id) / "layout.json"
                    if doc.block_count == 0 and layout_path.is_file():
                        doc.block_count = len(
                            layout_blocks_from_dict(load_layout_file(layout_path))
                        )
                    doc.error = str(e)[:2000]
                    doc.status = "partial" if doc.block_count > 0 else "failed"
                await session.commit()
            await self._emit(job_id, {"type": "error", "error": str(e)})
        finally:
            self.release(document_id)

    async def run_review(
        self,
        *,
        document_id: str,
        job_id: str,
    ) -> None:
        """Run LLM review on existing OCR artifacts (no page VL)."""
        if document_id not in self._running:
            self._running.add(document_id)
        from ocr_app.db.session import get_session_factory
        from ocr_app.library.service import mark_document_review_running

        factory = get_session_factory()
        try:
            async with self._document_semaphore():
                async with factory() as session:
                    doc = await get_document(session, document_id)
                    if not doc:
                        raise FileNotFoundError(f"document {document_id}")
                    job = await session.get(OcrJob, job_id)
                    if not job:
                        raise FileNotFoundError(f"job {job_id}")

                    ddir = doc_dir(document_id)
                    layout_path = ddir / "layout.json"
                    md_path = ddir / "content.md"
                    if not layout_path.is_file():
                        raise FileNotFoundError("layout.json not found; run OCR first")

                    layout_state = load_layout_file(layout_path)
                    all_blocks = layout_blocks_from_dict(layout_state)
                    if not all_blocks:
                        raise ValueError("layout has no blocks; run OCR first")

                    if md_path.is_file():
                        markdown = md_path.read_text(encoding="utf-8")
                    else:
                        markdown = blocks_to_markdown(all_blocks, title=doc.title)

                    pages_total = int(doc.pages or 0) or _pages_total(
                        0, layout_state, all_blocks
                    )
                    layout = blocks_to_layout(all_blocks, pages=pages_total or 1)
                    empty_pages = 0
                    present = pages_with_content(layout)
                    for p in range(1, int(pages_total) + 1):
                        if p not in present:
                            empty_pages += 1

                    await mark_document_review_running(session, doc)
                    job.status = "running"
                    job.current_page = 0
                    job.total_pages = 1
                    await session.commit()
                    await self._emit(
                        job_id,
                        {"type": "progress", "current_page": 0, "total_pages": 1},
                    )

                    review = await review_vision_markdown(
                        markdown,
                        pages=int(pages_total) or 1,
                        block_count=len(all_blocks),
                        empty_pages=empty_pages,
                    )
                    if SKIPPED_CONN_NOTE in markdown or SKIPPED_INSPECTION_NOTE in markdown:
                        review["needs_rerun"] = True
                        summary = str(review.get("summary") or "").strip()
                        notes: list[str] = []
                        if SKIPPED_CONN_NOTE in markdown:
                            notes.append("部分页面因网络中断被跳过")
                        if SKIPPED_INSPECTION_NOTE in markdown:
                            notes.append("部分页面因内容安全审核被跳过")
                        note = "；".join(notes)
                        review["summary"] = f"{summary}；{note}" if summary else note

                    write_artifacts(
                        ddir,
                        markdown=markdown,
                        layout=layout_state if layout_state.get("pages") else layout,
                        review=review,
                    )

                    job.status = "done"
                    job.current_page = 1
                    job.finished_at = datetime.now(timezone.utc)
                    await session.commit()

                    await mark_document_ready(
                        session,
                        doc,
                        pages=int(pages_total) or 1,
                        block_count=len(all_blocks),
                        review_score=float(review.get("score", 0)),
                        review_summary=str(review.get("summary") or ""),
                        vision_model=doc.vision_model or active_vision_model(),
                        needs_rerun=bool(review.get("needs_rerun")),
                    )
                    await self._emit(job_id, {"type": "done", "review": review})
        except asyncio.CancelledError:
            logger.warning("Review job cancelled for {}", document_id)
            await self._mark_abandoned(
                document_id,
                job_id,
                "质检已手动停止",
                running_status="review_running",
            )
            raise
        except Exception as e:
            logger.exception("Review job failed: {}", e)
            async with factory() as session:
                doc = await get_document(session, document_id)
                job = await session.get(OcrJob, job_id)
                if job:
                    job.status = "failed"
                    job.error = str(e)[:2000]
                    job.finished_at = datetime.now(timezone.utc)
                if doc and doc.status == "review_running":
                    # Fall back to ocr_done so user can retry review
                    doc.status = "ocr_done"
                    doc.error = str(e)[:2000]
                await session.commit()
            await self._emit(job_id, {"type": "error", "error": str(e)})
        finally:
            self.release(document_id)


job_manager = JobManager()
