"""LLM auto-review of VL-extracted Markdown."""
from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger

from ocr_app.config import settings
from ocr_app.ocr_core.dashscope_vl import dashscope_client

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)

REVIEW_SYSTEM = """你是文献 OCR/版面抽取质检员。根据抽取得到的 Markdown 与统计信息，评估抽取质量。
仅输出 JSON：
{"score":0.0到1.0,"issues":["..."],"summary":"一句话结论"}
评分参考：
- 1.0 结构清晰、正文完整
- 0.6–0.9 可用但有噪声/漏行
- <0.6 明显失败（大量乱码、空页过多、标题混乱）
不要编造原文未出现的史实；只评价抽取质量。"""


def _parse_review_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
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


async def review_vision_markdown(
    markdown: str,
    *,
    pages: int,
    block_count: int,
    empty_pages: int = 0,
) -> dict[str, Any]:
    threshold = float(settings.vision_review_threshold)
    excerpt = (markdown or "")[:12000]
    user = (
        f"页数={pages}，块数={block_count}，疑似空页={empty_pages}\n\n"
        f"--- Markdown 摘录 ---\n{excerpt}"
    )
    try:
        resp = await dashscope_client.chat(
            [
                {"role": "system", "content": REVIEW_SYSTEM},
                {"role": "user", "content": user},
            ],
            model=settings.chat_model,
        )
        content = ""
        if isinstance(resp, dict):
            choices = resp.get("choices") or []
            if choices:
                content = str((choices[0].get("message") or {}).get("content") or "")
        parsed = _parse_review_json(content)
    except Exception as e:
        logger.warning("vision review failed: {}", e)
        parsed = {
            "score": 0.55,
            "issues": [f"review_error:{e}"[:200]],
            "summary": "自动质检调用失败，默认中等分",
        }

    score = float(parsed["score"])
    needs_rerun = score < threshold
    return {
        "score": score,
        "issues": parsed.get("issues") or [],
        "summary": parsed.get("summary") or "",
        "needs_rerun": needs_rerun,
        "threshold": threshold,
        "model": settings.chat_model,
    }
