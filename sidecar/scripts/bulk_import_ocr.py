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


def set_concurrency(api: Api, *, page: int, doc: int, api_n: int) -> dict:
    return api.put(
        "/settings",
        {
            "ocr_page_concurrency": page,
            "ocr_document_concurrency": doc,
            "ocr_api_concurrency": api_n,
            "ocr_worker_processes": 1,
            "vision_pdf_max_pages": 10000,
        },
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
) -> None:
    targets = [d for d in docs if should_queue_ocr(d)]
    skipped = [d["id"] for d in docs if is_ocr_complete(d)]
    progress["ocr_skipped"] = len(skipped)
    progress["ocr_target_count"] = len(targets)
    progress["ocr_results"] = progress.get("ocr_results") or []
    progress["delivered"] = progress.get("delivered") or []
    write_progress(progress_path, progress)
    log(f"OCR skip={len(skipped)} queue={len(targets)}")

    set_concurrency(api, page=16, doc=1, api_n=16)
    page_conc = api_conc = 16
    progress["concurrency"] = {"page": page_conc, "api": api_conc, "doc": 1}

    already = {item.get("document_id") for item in progress["ocr_results"] if item.get("ok")}
    remaining = [d["id"] for d in targets if d["id"] not in already]

    probe_ids: list[str] = []
    if not skip_probe and remaining:
        probe_ids = [doc_id for doc_id in pick_probe_ids(targets, probe_n) if doc_id in set(remaining)]
        remaining = [doc_id for doc_id in remaining if doc_id not in set(probe_ids)]

    disconnect_hints = 0
    pages_done = 0
    probe_t0 = time.time()

    def process_one(doc_id: str, phase: str) -> dict:
        nonlocal disconnect_hints, pages_done
        doc = api.get(f"/library/documents/{doc_id}")
        if is_ocr_complete(doc):
            result = {
                "document_id": doc_id,
                "ok": True,
                "skipped": True,
                "status": doc.get("status"),
                "pages": doc.get("pages"),
                "block_count": doc.get("block_count"),
            }
            progress["ocr_results"].append(result)
            return result
        try:
            queue_ocr(api, doc_id)
        except Exception as exc:
            msg = str(exc).lower()
            if "409" not in msg and "already running" not in msg:
                result = {"document_id": doc_id, "ok": False, "error": str(exc), "phase": phase}
                progress["ocr_results"].append(result)
                return result
        info = wait_ocr(api, doc_id)
        ok = info.get("status") in DONE_OCR_STATUSES and info.get("error") not in {
            "timeout",
            "never_started",
        }
        result = {"document_id": doc_id, "ok": ok, "phase": phase, **info}
        err = str(info.get("error") or "").lower()
        if "disconnect" in err or "empty" in err:
            disconnect_hints += 1
        pages_done += int(info.get("pages") or 0)
        progress["ocr_results"].append(result)
        if deliver and ok:
            try:
                st = deliver_docs(api, [doc_id], submitter)
                item = (st.get("results") or [{}])[0]
                result["delivery"] = {"status": st.get("status"), "ok": bool(item.get("ok"))}
                if item.get("ok"):
                    progress["delivered"].append(doc_id)
            except Exception as exc:
                result["delivery"] = {"error": str(exc)}
        progress["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        done_n = sum(1 for x in progress["ocr_results"] if x.get("ok"))
        fail_n = sum(1 for x in progress["ocr_results"] if not x.get("ok") and not x.get("skipped"))
        progress["ocr_ok"] = done_n
        progress["ocr_fail"] = fail_n
        write_progress(progress_path, progress)
        return result

    if probe_ids:
        log(f"probe {len(probe_ids)} docs at page/api=16")
        for doc_id in probe_ids:
            result = process_one(doc_id, "probe")
            log(
                f"  probe {doc_id[:8]} {result.get('status')} "
                f"pages={result.get('pages')} err={result.get('error')}"
            )
        elapsed = max(1.0, time.time() - probe_t0)
        ppm = pages_done / (elapsed / 60.0)
        progress["probe"] = {
            "pages": pages_done,
            "seconds": elapsed,
            "pages_per_min": ppm,
            "disconnect_hints": disconnect_hints,
        }
        write_progress(progress_path, progress)
        log(f"probe throughput {ppm:.1f} pages/min hints={disconnect_hints}")
        if disconnect_hints == 0 and ppm > 8:
            page_conc = api_conc = 24
            set_concurrency(api, page=24, doc=1, api_n=24)
            log("ramp concurrency -> 24")
            progress["concurrency"] = {"page": 24, "api": 24, "doc": 1}
            write_progress(progress_path, progress)

    stable_ok = 0
    for index, doc_id in enumerate(remaining, 1):
        result = process_one(doc_id, "batch")
        log(
            f"[{index}/{len(remaining)}] {doc_id[:8]} {result.get('status')} "
            f"pages={result.get('pages')} blocks={result.get('block_count')} err={result.get('error')}"
        )
        if result.get("ok"):
            stable_ok += 1
        else:
            stable_ok = 0
        if page_conc == 24 and stable_ok >= 8 and disconnect_hints == 0:
            page_conc = api_conc = 32
            set_concurrency(api, page=32, doc=1, api_n=32)
            log("ramp concurrency -> 32")
            progress["concurrency"] = {"page": 32, "api": 32, "doc": 1}
            write_progress(progress_path, progress)
            stable_ok = 0
        if not result.get("ok") and result.get("error") in {"timeout", "never_started"}:
            if page_conc > 16:
                page_conc = api_conc = 16
                set_concurrency(api, page=16, doc=1, api_n=16)
                log("backoff concurrency -> 16")
                progress["concurrency"] = {"page": 16, "api": 16, "doc": 1}
                write_progress(progress_path, progress)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=r"F:\Source")
    parser.add_argument("--data-root", default=r"F:\Library")
    parser.add_argument("--base-url", default="http://127.0.0.1:18765")
    parser.add_argument("--import-concurrency", type=int, default=6)
    parser.add_argument("--probe-docs", type=int, default=8)
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
    log(
        f"health={health} provider={settings.get('llm_provider')} "
        f"key={settings.get('has_api_key')} max_pages={settings.get('vision_pdf_max_pages')}"
    )
    if Path(str(health.get("data_root") or "")).resolve() != Path(args.data_root).resolve():
        log(f"WARN sidecar data_root={health.get('data_root')} expected {args.data_root}")
    if settings.get("llm_provider") != "dashscope" or not settings.get("has_api_key"):
        log("ERROR: need dashscope + api key")
        return 2

    progress_path = Path(args.data_root) / "bulk_ocr_progress.json"
    progress: dict = {}
    if progress_path.is_file():
        try:
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            progress = {}
    progress.update(
        {
            "data_root": args.data_root,
            "source": args.source,
            "started_at": progress.get("started_at") or time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
    )

    pairs = iter_source_pdfs(Path(args.source))
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
        (Path(args.data_root) / "wenshi_bulk_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return 0 if progress["validation"].get("exit_code") in {None, 0} else 1

    if not args.skip_import and not args.ocr_only:
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
        (Path(args.data_root) / "wenshi_bulk_report.json").write_text(
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
