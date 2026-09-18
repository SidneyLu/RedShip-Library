"""LLM auto-review of VL-extracted Markdown."""
from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger

from ocr_app.config import active_chat_model, settings
from ocr_app.ocr_core.vl_client import get_vl_client

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_THINK_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_INSPECT_RE = re.compile(
    r"data_inspection|datainspection|inappropriate content", re.IGNORECASE
)

REVIEW_SYSTEM = """你是文献 OCR/版面抽取质检员。根据抽取得到的 Markdown 与统计信息，评估抽取质量。
只评价抽取质量，不要评价史料立场或敏感内容。
仅输出一个 JSON 对象，不要输出思考过程或其它文字：
{"score":0.0到1.0,"issues":["..."],"summary":"一句话结论"}
评分参考：
- 1.0 结构清晰、正文完整
- 0.6–0.9 可用但有噪声/漏行
- <0.6 明显失败（大量乱码、空页过多、标题混乱、正文被无关现代论文污染）
摘录可能只覆盖部分页；不要仅因摘录在句中结束就判定全书截断。
不要编造原文未出现的史实。"""


def _parse_review_json(text: str) -> dict[str, Any]:
    raw = _THINK_RE.sub("", text or "").strip()
    m = _JSON_FENCE_RE.search(raw)
    if m:
        raw = m.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return {"score": 0.5, "issues": ["review_parse_failed"], "summary": "质检结果解析失败"}
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return {"score": 0.5, "issues": ["review_parse_failed"], "summary": "质检结果解析失败"}
    if not isinstance(data, dict):
        return {"score": 0.5, "issues": ["review_invalid"], "summary": "质检结果无效"}
    try:
        score = float(data.get("score", 0.5))
    except (TypeError, ValueError):
        score = 0.5
    score = max(0.0, min(1.0, score))
    issues = data.get("issues") if isinstance(data.get("issues"), list) else []
    issues = [str(x) for x in issues][:20]
    summary = str(data.get("summary") or "").strip() or "已完成自动质检"
    return {"score": score, "issues": issues, "summary": summary}


def _excerpt(markdown: str, budget: int, *, mode: str) -> str:
    md = markdown or ""
    if not md:
        return ""
    if mode == "head" or len(md) <= budget:
        return md[:budget]
    third = max(400, budget // 3)
    head = md[:third]
    mid_start = max(0, (len(md) - third) // 2)
    mid = md[mid_start : mid_start + third]
    tail = md[-third:]
    return f"{head}\n\n--- [中段摘录] ---\n{mid}\n\n--- [末段摘录] ---\n{tail}"


def _is_inspection(err: BaseException | str) -> bool:
    return bool(_INSPECT_RE.search(str(err)))


def _is_soft_fail(parsed: dict[str, Any]) -> bool:
    issues = parsed.get("issues") or []
    return any(
        str(x) in {"review_parse_failed", "review_invalid"}
        or str(x).startswith("review_error:")
        for x in issues
    )


async def _chat_review(user: str) -> dict[str, Any]:
    resp = await get_vl_client().chat(
        [
            {"role": "system", "content": REVIEW_SYSTEM},
            {"role": "user", "content": user},
        ],
        model=active_chat_model(),
    )
    content = ""
    if isinstance(resp, dict):
        choices = resp.get("choices") or []
        if choices:
            content = str((choices[0].get("message") or {}).get("content") or "")
    return _parse_review_json(content)


async def review_vision_markdown(
    markdown: str,
    *,
    pages: int,
    block_count: int,
    empty_pages: int = 0,
) -> dict[str, Any]:
    threshold = float(settings.vision_review_threshold)
    attempts = (
        ("sample", 8000),
        ("sample", 4000),
        ("head", 1800),
    )
    parsed: dict[str, Any] | None = None
    last_err: str | None = None
    for mode, budget in attempts:
        excerpt = _excerpt(markdown, budget, mode=mode)
        user = (
            f"页数={pages}，块数={block_count}，疑似空页={empty_pages}\n"
            "只评价 OCR/版面抽取质量。历史文献中的战争、政治叙述不是违规内容。\n\n"
            f"--- Markdown 摘录 ---\n{excerpt}"
        )
        try:
            parsed = await _chat_review(user)
            last_err = None
            if not _is_soft_fail(parsed):
                break
            logger.warning("vision review soft-fail mode={} budget={}", mode, budget)
        except Exception as e:
            last_err = str(e)[:200]
            logger.warning("vision review failed mode={} budget={}: {}", mode, budget, e)
            if not _is_inspection(e) and mode != "head":
                # Network / 429: still retry remaining shorter attempts.
                continue
            parsed = {
                "score": 0.55,
                "issues": [f"review_error:{e}"[:200]],
                "summary": "自动质检调用失败，默认中等分",
            }
            if _is_inspection(e):
                continue

    if parsed is None:
        parsed = {
            "score": 0.55,
            "issues": [f"review_error:{last_err or 'unknown'}"],
            "summary": "自动质检调用失败，默认中等分",
        }

    # Inspection / parse failures are review-pipeline issues, not OCR failures.
    if _is_soft_fail(parsed) or (last_err and _is_inspection(last_err)):
        parsed = {
            "score": max(float(parsed.get("score") or 0), 0.65),
            "issues": list(parsed.get("issues") or [])[:18]
            + ["review_skipped_after_retry"],
            "summary": "质检接口审核/解析失败，已按可用抽取放行",
        }

    score = float(parsed["score"])
    needs_rerun = score < threshold
    return {
        "score": score,
        "issues": parsed.get("issues") or [],
        "summary": parsed.get("summary") or "",
        "needs_rerun": needs_rerun,
        "threshold": threshold,
        "model": active_chat_model(),
    }
