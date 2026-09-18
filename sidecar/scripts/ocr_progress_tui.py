#!/usr/bin/env python3
"""Live OCR progress panel for a terminal (refreshes in place)."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path


def snapshot(data_root: Path) -> dict:
    db = data_root / "library.db"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    status = dict(conn.execute("select status, count(*) from documents group by status"))
    total = sum(status.values()) or 1
    running = conn.execute(
        """
        select id, substr(coalesce(title,''),1,36), pages, block_count,
               coalesce((select current_page from ocr_jobs j
                         where j.document_id=d.id and j.status='running'
                         order by rowid desc limit 1), 0)
        from documents d where status='ocr_running'
        order by title limit 12
        """
    ).fetchall()
    page_jobs = dict(
        conn.execute(
            "select status, count(*) from ocr_page_jobs "
            "where status in ('pending','running','done','failed') group by status"
        )
    )
    conn.close()

    doneish = status.get("ocr_done", 0) + status.get("ready", 0)
    touched = doneish + status.get("partial", 0) + status.get("needs_rerun", 0)
    prog = {}
    pp = data_root / "reports" / "bulk_ocr_progress.json"
    if not pp.is_file():
        pp = data_root / "bulk_ocr_progress.json"
    if pp.is_file():
        try:
            prog = json.loads(pp.read_text(encoding="utf-8"))
        except Exception:
            prog = {}

    return {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": status,
        "total": total,
        "doneish": doneish,
        "touched": touched,
        "running_rows": running,
        "page_jobs": page_jobs,
        "concurrency": prog.get("concurrency"),
        "ocr_ok": prog.get("ocr_ok"),
        "ocr_fail": prog.get("ocr_fail"),
        "updated_at": prog.get("updated_at"),
    }


def render(s: dict) -> str:
    st = s["status"]
    total = s["total"]
    done_pct = 100.0 * s["doneish"] / total
    touch_pct = 100.0 * s["touched"] / total
    bar_w = 40
    filled = int(bar_w * s["doneish"] / total)
    bar = "█" * filled + "░" * (bar_w - filled)

    lines = [
        f" OCR Library Progress  {s['ts']}",
        "─" * 64,
        f" [{bar}] {done_pct:5.1f}%  done+ready={s['doneish']}/{total}",
        f" touched={s['touched']} ({touch_pct:.1f}%)  pending={st.get('pending',0)}  "
        f"running={st.get('ocr_running',0)}  partial={st.get('partial',0)}  "
        f"failed={st.get('failed',0)}  needs_rerun={st.get('needs_rerun',0)}",
        f" page_jobs pending={s['page_jobs'].get('pending',0)}  "
        f"running={s['page_jobs'].get('running',0)}  done={s['page_jobs'].get('done',0)}",
        f" bulk ok={s.get('ocr_ok')} fail={s.get('ocr_fail')}  "
        f"concurrency={s.get('concurrency')}  progress_at={s.get('updated_at')}",
        "─" * 64,
        " in-flight (sample):",
    ]
    if not s["running_rows"]:
        lines.append("   (none)")
    else:
        for doc_id, title, pages, blocks, cur in s["running_rows"]:
            pages = int(pages or 0)
            cur = int(cur or 0)
            pct = (100.0 * cur / pages) if pages else 0.0
            lines.append(
                f"   {doc_id[:8]}  p{cur}/{pages or '?'} ({pct:4.0f}%)  "
                f"blocks={blocks or 0}  {title}"
            )
    lines.append("─" * 64)
    lines.append(" Ctrl+C to stop this panel (OCR keeps running)")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", default="/root/autodl-tmp/Library")
    parser.add_argument("--interval", type=float, default=3.0)
    args = parser.parse_args()
    data_root = Path(args.data_root)
    while True:
        try:
            text = render(snapshot(data_root))
        except Exception as exc:
            text = f"progress error: {exc}"
        # Clear screen + home cursor
        os.write(1, b"\033[2J\033[H")
        print(text, flush=True)
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nstopped panel", flush=True)
        raise SystemExit(0)
