"""Import F:\\Source province PDFs, OCR unfinished docs, optionally deliver.

Talks to a running sidecar over HTTP so SQLite stays single-writer.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKIP_OCR_STATUSES = {"ready", "ocr_done"}
OCR_QUEUE_STATUSES = {"pending", "partial", "failed", "needs_rerun"}
DONE_OCR_STATUSES = {"ready", "ocr_done", "partial"}


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

    def patch(self, path: str, body=None, timeout: float | None = None):
        return self.req("PATCH", path, body, timeout=timeout)


def iter_source_pdfs(source: Path) -> list[tuple[str, Path]]:
    items: list[tuple[str, Path]] = []
    for folder in sorted(p for p in source.iterdir() if p.is_dir()):
        series = folder.name.strip()
        if not series:
            continue
        for pdf in sorted(folder.glob("*.pdf")):
            if pdf.is_file():
                items.append((series, pdf))
    return items


def list_all_docs(api: Api) -> list[dict]:
    data = api.get("/library/documents?limit=100000&sort=title")
    return list(data.get("items") or [])


def is_ocr_complete(doc: dict) -> bool:
    status = doc.get("status")
    pages = int(doc.get("pages") or 0)
    blocks = int(doc.get("block_count") or 0)
    if status in SKIP_OCR_STATUSES and (pages <= 0 or blocks > 0):
        return True
    if status == "ready":
        return True
    return False


def should_queue_ocr(doc: dict) -> bool:
    if is_ocr_complete(doc):
        return False
    status = doc.get("status")
    if status in {"ocr_running", "review_running"}:
        return False
    if status in OCR_QUEUE_STATUSES:
        return True
    if status in SKIP_OCR_STATUSES:
        pages = int(doc.get("pages") or 0)
        blocks = int(doc.get("block_count") or 0)
        return pages > 0 and blocks == 0
    return False


def write_progress(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def import_one(api: Api, series: str, pdf: Path) -> dict:
    doc = api.post(
        "/library/import",
        {
            "pdf_path": str(pdf),
            "title": pdf.stem,
            "series": None if series == "未分类" else series,
            "copy_file": True,
            "run_ocr": False,
        },
        timeout=600,
    ).get("document") or {}
    doc_id = doc.get("id")
    if series and series != "未分类" and doc_id and doc.get("series") != series:
        try:
            doc = api.patch(f"/library/documents/{doc_id}", {"series": series})
        except Exception as exc:
            return {"ok": False, "path": str(pdf), "error": f"patch series: {exc}"}
    return {
        "ok": True,
        "document_id": doc_id,
        "status": doc.get("status"),
        "series": doc.get("series") or series,
        "path": str(pdf),
    }


def ensure_folders(api: Api, names: set[str]) -> dict:
    existing = {f["name"] for f in (api.get("/library/folders").get("folders") or [])}
    created, skipped = [], []
    for name in sorted(names):
        if name == "未分类":
            continue
        if name in existing:
            skipped.append(name)
            continue
        api.post("/library/folders", {"name": name})
        created.append(name)
        existing.add(name)
    return {"created": created, "skipped": skipped}


def set_concurrency(
    api: Api,
    *,
    page: int,
    doc: int,
    api_n: int,
    workers: int = 8,
) -> dict:
    return api.put(
        "/settings",
        {
            "ocr_page_concurrency": page,
            "ocr_document_concurrency": doc,
            "ocr_api_concurrency": api_n,
            "ocr_worker_processes": workers,
            "vision_pdf_dpi": 300,
            "vision_pdf_max_pages": 10000,
        },
    )


def queue_ocr_batch(api: Api, doc_ids: list[str]) -> dict:
    return api.post(
        "/library/ocr",
        {"document_ids": doc_ids, "force": False, "auto_review": False},
    )


def queue_ocr(api: Api, doc_id: str) -> dict:
    return api.post(
        f"/library/documents/{doc_id}/ocr",
        {"force": False, "auto_review": False},
    )


def wait_ocr(api: Api, doc_id: str, *, stall_s: int = 1200, timeout_s: int = 6 * 3600) -> dict:
    t0 = time.time()
    seen_running = False
    last_key = None
    stall_t = time.time()
    while True:
        doc = api.get(f"/library/documents/{doc_id}")
        job = doc.get("ocr_job") or {}
        status = doc.get("status")
        running = status == "ocr_running" or job.get("status") in {"running", "queued"}
        key = (status, job.get("current_page"), job.get("status"), doc.get("block_count"))
        if key != last_key:
            last_key = key
            stall_t = time.time()
            log(
                f"  {doc_id[:8]} status={status} job={job.get('status')} "
                f"page={job.get('current_page')}/{job.get('total_pages') or doc.get('pages')} "
                f"blocks={doc.get('block_count')}"
            )
        if running:
            seen_running = True
        elif seen_running:
            time.sleep(1.5)
            doc2 = api.get(f"/library/documents/{doc_id}")
            job2 = doc2.get("ocr_job") or {}
            if doc2.get("status") == "ocr_running" or job2.get("status") in {"running", "queued"}:
                continue
            return {
                "status": doc2.get("status"),
                "pages": doc2.get("pages"),
                "block_count": doc2.get("block_count"),
                "error": doc2.get("error"),
                "elapsed": time.time() - t0,
            }
        elif time.time() - t0 > 120 and not seen_running:
            return {
                "status": status,
                "pages": doc.get("pages"),
                "block_count": doc.get("block_count"),
                "error": doc.get("error") or "never_started",
                "elapsed": time.time() - t0,
            }
        if running and time.time() - stall_t > stall_s:
            log(f"  STALL {doc_id[:8]} abandon+requeue")
            try:
                api.post("/library/abandon-ocr", {"document_ids": [doc_id], "reason": "stall"})
            except Exception as exc:
                log(f"  abandon fail: {exc}")
            time.sleep(2)
            try:
                queue_ocr(api, doc_id)
            except Exception as exc:
                log(f"  requeue fail: {exc}")
            stall_t = time.time()
            seen_running = False
        if time.time() - t0 > timeout_s:
            return {
                "status": "timeout",
                "pages": doc.get("pages"),
                "block_count": doc.get("block_count"),
                "error": "timeout",
                "elapsed": time.time() - t0,
            }
        time.sleep(15)


def deliver_docs(api: Api, doc_ids: list[str], submitter: str) -> dict:
    if not doc_ids:
        return {"status": "skipped", "results": []}
    # Process in chunks to avoid huge in-memory jobs
    all_results: list[dict] = []
    chunk = 50
    for i in range(0, len(doc_ids), chunk):
        part = doc_ids[i : i + chunk]
        job = api.post(
            "/library/process/wenshi",
            {
                "document_ids": part,
                "split_papers": True,
                "extract_entities": True,
                "use_model": False,
                "export": True,
                "submitter": submitter,
            },
        )
        job_id = job["job_id"]
        while True:
            st = api.get(f"/delivery-jobs/{job_id}")
            log(f"  delivery chunk {i // chunk + 1} {st.get('status')} {st.get('current')}/{st.get('total')}")
            if st.get("status") != "running":
                all_results.extend(st.get("results") or [])
                break
            time.sleep(5)
    return {"status": "completed", "results": all_results, "total": len(doc_ids)}


def validate_submit(submit_dir: Path) -> dict:
    validator = ROOT / "requirements" / "校验.py"
    if not validator.is_file() or not submit_dir.exists():
        return {"exit_code": None, "stdout": "", "skipped": True}
    proc = subprocess.run(
        [sys.executable, str(validator), str(submit_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return {
        "exit_code": proc.returncode,
        "stdout": (proc.stdout or "")[-4000:],
        "stderr": (proc.stderr or "")[-2000:],
    }


def pick_probe_ids(docs: list[dict], n: int) -> list[str]:
    scored: list[tuple[int, str]] = []
    for doc in docs:
        if not should_queue_ocr(doc):
            continue
        pages = int(doc.get("pages") or 0)
        if 40 <= pages <= 180:
            scored.append((abs(pages - 100), doc["id"]))
        elif pages > 0:
            scored.append((200 + abs(pages - 100), doc["id"]))
        else:
            scored.append((400, doc["id"]))
    scored.sort()
    return [doc_id for _, doc_id in scored[:n]]


def run_import(
    api: Api,
    pairs: list[tuple[str, Path]],
    workers: int,
    progress: dict,
    progress_path: Path,
) -> None:
    ensure_folders(api, {series for series, _ in pairs})
    imported = progress.setdefault("imported", [])
    errors = progress.setdefault("import_errors", [])
    done_paths = {item.get("path") for item in imported if item.get("ok")}
    todo = [(series, pdf) for series, pdf in pairs if str(pdf) not in done_paths]
    log(f"import {len(todo)} files ({len(done_paths)} already recorded), workers={workers}")
    if not todo:
        return

    def work(item: tuple[str, Path]) -> dict:
        series, pdf = item
        try:
            return import_one(api, series, pdf)
        except Exception as exc:
            return {"ok": False, "path": str(pdf), "series": series, "error": str(exc)}

    finished = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(work, item): item for item in todo}
        for fut in as_completed(futs):
            result = fut.result()
            finished += 1
            if result.get("ok"):
                imported.append(result)
            else:
                errors.append(result)
            if finished % 50 == 0 or finished == len(todo):
                progress["import_done"] = len(done_paths) + finished
                progress["import_total"] = len(pairs)
                progress["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                write_progress(progress_path, progress)
                ok_n = sum(1 for x in imported if x.get("ok"))
                log(f"import {finished}/{len(todo)} ok={ok_n} err={len(errors)}")


def run_ocr_phase(
    api: Api,
    docs: list[dict],
    *,
    progress: dict,
    progress_path: Path,
    probe_n: int,
    deliver: bool,
    submitter: str,
    skip_probe: bool,
    page_conc: int = 64,
    doc_conc: int = 16,
    api_conc: int = 256,
    workers: int = 8,
    ramp: bool = True,
) -> None:
    """Multi-document OCR: keep up to doc_conc jobs in flight via /library/ocr."""
    targets = [d for d in docs if should_queue_ocr(d)]
    skipped = [d["id"] for d in docs if is_ocr_complete(d)]
    progress["ocr_skipped"] = len(skipped)
    progress["ocr_target_count"] = len(targets)
    progress["ocr_results"] = progress.get("ocr_results") or []
    progress["delivered"] = progress.get("delivered") or []
    write_progress(progress_path, progress)
    log(f"OCR skip={len(skipped)} queue={len(targets)}")

    ladders = [
        {"page": page_conc, "doc": doc_conc, "api": api_conc, "workers": workers},
        {
            "page": max(page_conc, 128),
            "doc": max(doc_conc, 32),
            "api": max(api_conc, 512),
            "workers": workers,
        },
        {"page": 128, "doc": 32, "api": 800, "workers": workers},
        {"page": 256, "doc": 48, "api": 1200, "workers": workers},
    ]
    if not ramp:
        ladders = [ladders[0]]

    cur = ladders[0]
    set_concurrency(api, page=cur["page"], doc=cur["doc"], api_n=cur["api"], workers=cur["workers"])
    progress["concurrency"] = dict(cur)
    write_progress(progress_path, progress)
    log(f"concurrency start {cur}")

    # Trust live library status only — progress "ok" can be stale after crashes.
    remaining = [d["id"] for d in targets]

    if not skip_probe and remaining and probe_n > 0:
        probe_ids = [doc_id for doc_id in pick_probe_ids(targets, probe_n) if doc_id in set(remaining)]
        remaining = [doc_id for doc_id in remaining if doc_id not in set(probe_ids)]
        probe_t0 = time.time()
        pages_done = 0
        disconnect_hints = 0
        log(f"probe {len(probe_ids)} docs at {cur}")
        for doc_id in probe_ids:
            try:
                queue_ocr(api, doc_id)
            except Exception as exc:
                msg = str(exc).lower()
                if "409" not in msg and "already running" not in msg:
                    progress["ocr_results"].append(
                        {"document_id": doc_id, "ok": False, "error": str(exc), "phase": "probe"}
                    )
                    continue
            info = wait_ocr(api, doc_id, stall_s=600, timeout_s=3600)
            ok = info.get("status") in DONE_OCR_STATUSES and info.get("error") not in {
                "timeout",
                "never_started",
            }
            err = str(info.get("error") or "").lower()
            if "disconnect" in err or "429" in err:
                disconnect_hints += 1
            pages_done += int(info.get("pages") or 0)
            progress["ocr_results"].append(
                {"document_id": doc_id, "ok": ok, "phase": "probe", **info}
            )
            write_progress(progress_path, progress)
            log(
                f"  probe {doc_id[:8]} {info.get('status')} "
                f"pages={info.get('pages')} err={info.get('error')}"
            )
        elapsed = max(1.0, time.time() - probe_t0)
        ppm = pages_done / (elapsed / 60.0)
        progress["probe"] = {
            "pages": pages_done,
            "seconds": elapsed,
            "pages_per_min": ppm,
            "disconnect_hints": disconnect_hints,
            "concurrency": dict(cur),
        }
        write_progress(progress_path, progress)
        log(f"probe throughput {ppm:.1f} pages/min hints={disconnect_hints}")
        if disconnect_hints == 0 and ppm > 8 and len(ladders) > 1:
            cur = ladders[1]
            set_concurrency(
                api, page=cur["page"], doc=cur["doc"], api_n=cur["api"], workers=cur["workers"]
            )
            progress["concurrency"] = dict(cur)
            write_progress(progress_path, progress)
            log(f"ramp concurrency -> {cur}")

    in_flight: dict[str, float] = {}
    queue = list(remaining)
    ladder_idx = 0
    for i, level in enumerate(ladders):
        if level == cur:
            ladder_idx = i
            break
    stable_ok = 0
    wave_t0 = time.time()
    done_set = {item.get("document_id") for item in progress["ocr_results"] if item.get("ok")}

    def record_finished(doc_id: str, doc: dict) -> None:
        nonlocal stable_ok
        job = doc.get("ocr_job") or {}
        status = doc.get("status")
        err = doc.get("error")
        job_status = job.get("status")
        running = status == "ocr_running" or job_status in {"running", "queued"}
        # Only finalize on terminal document statuses (ignore stale job rows).
        if running or status not in DONE_OCR_STATUSES | {"failed"}:
            return
        ok = status in DONE_OCR_STATUSES
        info = {
            "document_id": doc_id,
            "ok": bool(ok and err not in {"timeout", "never_started"}),
            "phase": "batch",
            "status": status,
            "pages": doc.get("pages"),
            "block_count": doc.get("block_count"),
            "error": err,
            "elapsed": time.time() - in_flight.get(doc_id, time.time()),
        }
        progress["ocr_results"].append(info)
        if info["ok"]:
            done_set.add(doc_id)
            stable_ok += 1
        else:
            stable_ok = 0
        if deliver and info["ok"]:
            try:
                st = deliver_docs(api, [doc_id], submitter)
                item = (st.get("results") or [{}])[0]
                info["delivery"] = {"status": st.get("status"), "ok": bool(item.get("ok"))}
                if item.get("ok"):
                    progress["delivered"].append(doc_id)
            except Exception as exc:
                info["delivery"] = {"error": str(exc)}
        progress["ocr_ok"] = sum(1 for x in progress["ocr_results"] if x.get("ok"))
        progress["ocr_fail"] = sum(
            1 for x in progress["ocr_results"] if not x.get("ok") and not x.get("skipped")
        )
        progress["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        write_progress(progress_path, progress)
        log(
            f"[{progress['ocr_ok']}/{progress['ocr_target_count']}] {doc_id[:8]} "
            f"{status} pages={doc.get('pages')} blocks={doc.get('block_count')} err={err}"
        )

    while queue or in_flight:
        slots = max(0, int(cur["doc"]) - len(in_flight))
        batch: list[str] = []
        while slots > 0 and queue:
            doc_id = queue.pop(0)
            if doc_id in done_set or doc_id in in_flight:
                continue
            batch.append(doc_id)
            slots -= 1
        if batch:
            try:
                resp = queue_ocr_batch(api, batch)
                skipped_ids = set(resp.get("skipped") or [])
                now = time.time()
                for doc_id in batch:
                    in_flight[doc_id] = now
                    if doc_id in skipped_ids:
                        pass
                log(f"queued {len(batch)} in_flight={len(in_flight)} remaining={len(queue)}")
            except Exception as exc:
                log(f"batch queue fail: {exc}; fallback single")
                for doc_id in batch:
                    try:
                        queue_ocr(api, doc_id)
                        in_flight[doc_id] = time.time()
                    except Exception as exc2:
                        msg = str(exc2).lower()
                        if "409" in msg or "already running" in msg:
                            in_flight[doc_id] = time.time()
                        else:
                            progress["ocr_results"].append(
                                {
                                    "document_id": doc_id,
                                    "ok": False,
                                    "error": str(exc2),
                                    "phase": "batch",
                                }
                            )

        # Heartbeat: page-level progress for in-flight docs (so logs are not silent).
        if in_flight:
            hearts = []
            for doc_id in list(in_flight)[:8]:
                try:
                    doc = api.get(f"/library/documents/{doc_id}")
                except Exception:
                    continue
                job = doc.get("ocr_job") or {}
                hearts.append(
                    f"{doc_id[:8]} {doc.get('status')} "
                    f"p{job.get('current_page') or 0}/{job.get('total_pages') or doc.get('pages') or '?'} "
                    f"blocks={doc.get('block_count') or 0}"
                )
            if hearts:
                log(
                    f"progress in_flight={len(in_flight)} remaining={len(queue)} | "
                    + " || ".join(hearts)
                )

        finished_ids: list[str] = []
        for doc_id in list(in_flight):
            try:
                doc = api.get(f"/library/documents/{doc_id}")
            except Exception as exc:
                log(f"  poll fail {doc_id[:8]}: {exc}")
                continue
            status = doc.get("status")
            job = doc.get("ocr_job") or {}
            running = status == "ocr_running" or job.get("status") in {"running", "queued"}
            if running:
                if time.time() - in_flight[doc_id] > 2400:
                    log(f"  STALL {doc_id[:8]} abandon+requeue")
                    try:
                        api.post(
                            "/library/abandon-ocr",
                            {"document_ids": [doc_id], "reason": "stall"},
                        )
                    except Exception as exc:
                        log(f"  abandon fail: {exc}")
                    time.sleep(1)
                    try:
                        queue_ocr(api, doc_id)
                    except Exception as exc:
                        log(f"  requeue fail: {exc}")
                    in_flight[doc_id] = time.time()
                continue
            # Still pending/partial-in-progress etc. — keep in flight.
            if status not in (DONE_OCR_STATUSES | {"failed"}):
                # If stuck pending too long after enqueue, re-send OCR once.
                if status == "pending" and time.time() - in_flight[doc_id] > 180:
                    try:
                        queue_ocr(api, doc_id)
                        in_flight[doc_id] = time.time()
                        log(f"  re-enqueue pending {doc_id[:8]}")
                    except Exception as exc:
                        msg = str(exc).lower()
                        if "409" in msg or "already" in msg:
                            in_flight[doc_id] = time.time()
                        else:
                            log(f"  re-enqueue fail {doc_id[:8]}: {exc}")
                continue
            record_finished(doc_id, doc)
            finished_ids.append(doc_id)
        for doc_id in finished_ids:
            in_flight.pop(doc_id, None)

        if (
            ramp
            and ladder_idx + 1 < len(ladders)
            and stable_ok >= 16
            and time.time() - wave_t0 > 120
        ):
            ladder_idx += 1
            cur = ladders[ladder_idx]
            set_concurrency(
                api, page=cur["page"], doc=cur["doc"], api_n=cur["api"], workers=cur["workers"]
            )
            progress["concurrency"] = dict(cur)
            write_progress(progress_path, progress)
            log(f"ramp concurrency -> {cur}")
            stable_ok = 0
            wave_t0 = time.time()

        if in_flight or queue:
            time.sleep(8)

    progress["ocr_ok"] = sum(1 for x in progress["ocr_results"] if x.get("ok"))
    progress["ocr_fail"] = sum(
        1 for x in progress["ocr_results"] if not x.get("ok") and not x.get("skipped")
    )
    write_progress(progress_path, progress)
    log(f"OCR wave done ok={progress['ocr_ok']} fail={progress['ocr_fail']}")

    # Rescan until library has no queueable docs (handles dropped in-flight races).
    for round_i in range(1, 50):
        docs2 = list_all_docs(api)
        left = [d["id"] for d in docs2 if should_queue_ocr(d)]
        # also include anything still running so we wait
        running_ids = [
            d["id"]
            for d in docs2
            if d.get("status") in {"ocr_running", "review_running"}
            or (d.get("ocr_job") or {}).get("status") in {"running", "queued"}
        ]
        log(f"rescan#{round_i} queueable={len(left)} running={len(running_ids)}")
        progress["library_status"] = {}
        for d in docs2:
            k = str(d.get("status") or "")
            progress["library_status"][k] = progress["library_status"].get(k, 0) + 1
        write_progress(progress_path, progress)
        if not left and not running_ids:
            break
        if not left and running_ids:
            # wait for running to finish
            time.sleep(30)
            continue
        # Re-enter a tight wave for remaining ids only
        queue = list(left)
        in_flight = {}
        while queue or in_flight:
            slots = max(0, int(cur["doc"]) - len(in_flight))
            batch = []
            while slots > 0 and queue:
                doc_id = queue.pop(0)
                if doc_id in in_flight:
                    continue
                batch.append(doc_id)
                slots -= 1
            if batch:
                try:
                    api.post(
                        "/library/ocr",
                        {"document_ids": batch, "force": False, "auto_review": False},
                    )
                except Exception as exc:
                    log(f"rescan batch fail: {exc}")
                now = time.time()
                for doc_id in batch:
                    in_flight[doc_id] = now
                log(f"rescan queued {len(batch)} in_flight={len(in_flight)} remaining={len(queue)}")
            finished_ids = []
            for doc_id in list(in_flight):
                try:
                    doc = api.get(f"/library/documents/{doc_id}")
                except Exception as exc:
                    log(f"  poll fail {doc_id[:8]}: {exc}")
                    continue
                status = doc.get("status")
                job = doc.get("ocr_job") or {}
                running = status == "ocr_running" or job.get("status") in {"running", "queued"}
                if running:
                    if time.time() - in_flight[doc_id] > 2400:
                        try:
                            api.post(
                                "/library/abandon-ocr",
                                {"document_ids": [doc_id], "reason": "stall"},
                            )
                            queue_ocr(api, doc_id)
                        except Exception as exc:
                            log(f"  stall fix fail: {exc}")
                        in_flight[doc_id] = time.time()
                    continue
                if status not in (DONE_OCR_STATUSES | {"failed"}):
                    if status == "pending" and time.time() - in_flight[doc_id] > 180:
                        try:
                            queue_ocr(api, doc_id)
                        except Exception:
                            pass
                        in_flight[doc_id] = time.time()
                    continue
                record_finished(doc_id, doc)
                finished_ids.append(doc_id)
            for doc_id in finished_ids:
                in_flight.pop(doc_id, None)
            if in_flight or queue:
                time.sleep(8)
    log(
        f"OCR phase done ok={progress.get('ocr_ok')} fail={progress.get('ocr_fail')} "
        f"status={progress.get('library_status')}"
    )



def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=r"F:\Source")
    parser.add_argument("--data-root", default="/root/autodl-tmp/Library")
    parser.add_argument("--base-url", default="http://127.0.0.1:18765")
    parser.add_argument("--import-concurrency", type=int, default=6)
    parser.add_argument("--probe-docs", type=int, default=8)
    parser.add_argument("--page-concurrency", type=int, default=64)
    parser.add_argument("--doc-concurrency", type=int, default=16)
    parser.add_argument("--api-concurrency", type=int, default=256)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--no-ramp", action="store_true")
    parser.add_argument("--skip-import", action="store_true")
    parser.add_argument("--skip-ocr", action="store_true")
    parser.add_argument("--skip-probe", action="store_true")
    parser.add_argument("--deliver", action="store_true")
    parser.add_argument("--submitter", default="未命名")
    parser.add_argument("--import-only", action="store_true")
    parser.add_argument("--ocr-only", action="store_true")
    parser.add_argument("--deliver-only", action="store_true")
    args = parser.parse_args()

    api = Api(args.base_url)
    health = api.get("/health")
    settings = api.get("/settings")
    provider = str(settings.get("llm_provider") or "dashscope").strip().lower()
    has_key = bool(
        settings.get("has_openai_api_key")
        if provider == "openai_responses"
        else settings.get("has_api_key")
    )
    log(
        f"health={health} provider={provider} "
        f"key={has_key} max_pages={settings.get('vision_pdf_max_pages')}"
    )
    if Path(str(health.get("data_root") or "")).resolve() != Path(args.data_root).resolve():
        log(f"WARN sidecar data_root={health.get('data_root')} expected {args.data_root}")
    if provider not in {"dashscope", "openai_responses"} or not has_key:
        log("ERROR: need configured llm_provider + api key")
        return 2

    progress_path = Path(args.data_root) / "reports" / "bulk_ocr_progress.json"
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_progress = Path(args.data_root) / "bulk_ocr_progress.json"
    load_from = progress_path if progress_path.is_file() else legacy_progress
    progress: dict = {}
    if load_from.is_file():
        try:
            progress = json.loads(load_from.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            progress = {}
    progress.update(
        {
            "data_root": args.data_root,
            "source": args.source,
            "started_at": progress.get("started_at") or time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
    )

    pairs = iter_source_pdfs(Path(args.source)) if Path(args.source).is_dir() else []
    log(f"source pdfs={len(pairs)}")
    progress["source_total"] = len(pairs)
    write_progress(progress_path, progress)

    if args.deliver_only:
        docs = list_all_docs(api)
        ready = [d["id"] for d in docs if d.get("status") in DONE_OCR_STATUSES]
        log(f"deliver-only n={len(ready)}")
        st = deliver_docs(api, ready, args.submitter)
        progress["delivery"] = {"status": st.get("status"), "total": st.get("total")}
        submit_dir = Path(args.data_root) / "提交" / args.submitter
        progress["validation"] = validate_submit(submit_dir)
        write_progress(progress_path, progress)
        log(f"validate {progress['validation']}")
        report = {
            "progress": progress_path.as_posix(),
            "delivery": progress.get("delivery"),
            "validation": progress.get("validation"),
        }
        (Path(args.data_root) / "reports" / "wenshi_bulk_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return 0 if progress["validation"].get("exit_code") in {None, 0} else 1

    if not args.skip_import and not args.ocr_only:
        if not pairs:
            log("skip import: source missing or empty")
        else:
            run_import(api, pairs, args.import_concurrency, progress, progress_path)

    if args.import_only:
        write_progress(progress_path, progress)
        return 0 if not progress.get("import_errors") else 1

    docs = list_all_docs(api)
    progress["library_total"] = len(docs)
    status_counts: dict[str, int] = {}
    for doc in docs:
        key = str(doc.get("status") or "")
        status_counts[key] = status_counts.get(key, 0) + 1
    progress["library_status"] = status_counts
    write_progress(progress_path, progress)
    log(f"library total={len(docs)} status={status_counts}")

    if not args.skip_ocr:
        run_ocr_phase(
            api,
            docs,
            progress=progress,
            progress_path=progress_path,
            probe_n=args.probe_docs,
            deliver=args.deliver,
            submitter=args.submitter,
            skip_probe=args.skip_probe,
            page_conc=args.page_concurrency,
            doc_conc=args.doc_concurrency,
            api_conc=args.api_concurrency,
            workers=args.workers,
            ramp=not args.no_ramp,
        )

    if args.deliver:
        docs = list_all_docs(api)
        delivered = set(progress.get("delivered") or [])
        pending = [
            d["id"]
            for d in docs
            if d.get("status") in DONE_OCR_STATUSES and d["id"] not in delivered
        ]
        if pending:
            log(f"final delivery n={len(pending)}")
            st = deliver_docs(api, pending, args.submitter)
            progress["final_delivery"] = {"status": st.get("status"), "total": st.get("total")}
        submit_dir = Path(args.data_root) / "提交" / args.submitter
        progress["validation"] = validate_submit(submit_dir)
        write_progress(progress_path, progress)
        (Path(args.data_root) / "reports" / "wenshi_bulk_report.json").write_text(
            json.dumps(
                {
                    "validation": progress.get("validation"),
                    "ocr_ok": progress.get("ocr_ok"),
                    "ocr_fail": progress.get("ocr_fail"),
                    "library_total": progress.get("library_total"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        log(f"validate {progress.get('validation')}")

    write_progress(progress_path, progress)
    log(f"progress -> {progress_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
