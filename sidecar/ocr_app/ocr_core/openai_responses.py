"""OpenAI Responses API client for VL layout + chat review (vLLM-compatible)."""
from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import random
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

import httpx
from loguru import logger
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from ocr_app.config import active_chat_model, active_vision_model, settings
from ocr_app.ocr_core.dashscope_vl import _is_transient_disconnect
from ocr_app.ocr_core.vl_rate_limiter import vl_limiter

_VL_CALL_TIMEOUT_S = 600
_CHAT_CALL_TIMEOUT_S = 180
_RETRY_STATUS = {429, 500, 502, 503, 504}
_METRICS_RING: list[dict[str, Any]] = []
_METRICS_LOCK = asyncio.Lock()
_METRICS_MAX = 200


def vl_call_metrics_snapshot(limit: int = 100) -> list[dict[str, Any]]:
    """Recent VL/chat call timings for the status panel."""
    return list(_METRICS_RING[-max(1, limit) :])


def _metrics_path() -> Path:
    from ocr_app.library.paths import logs_dir

    return logs_dir() / "vl_call_metrics.jsonl"


def _record_vl_metric(event: dict[str, Any]) -> None:
    _METRICS_RING.append(event)
    if len(_METRICS_RING) > _METRICS_MAX:
        del _METRICS_RING[: len(_METRICS_RING) - _METRICS_MAX]
    try:
        with _metrics_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass
STRICT_BBOX_HINT = (
    "重要：每个文字块必须有独立、紧贴文字的 bbox。"
    "禁止输出整页 [0,0,1000,1000]。标题与正文必须分开。"
)

T = TypeVar("T")


class OpenAIResponsesAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _is_retryable(exc: BaseException) -> bool:
    if _is_transient_disconnect(exc):
        return True
    if isinstance(exc, (httpx.HTTPError, asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return True
    if isinstance(exc, OpenAIResponsesAPIError):
        if exc.status_code in _RETRY_STATUS:
            return True
        msg = str(exc).lower()
        return any(
            token in msg
            for token in (
                "timeout",
                "temporarily",
                "connection",
                "unavailable",
                "rate",
                "throttl",
                "429",
                "502",
                "503",
                "504",
            )
        )
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "timeout",
            "connection",
            "unavailable",
            "429",
            "502",
            "503",
            "504",
        )
    )


def _base_url() -> str:
    return (settings.openai_base_url or "http://127.0.0.1:8000/v1").rstrip("/")


def _auth_header() -> str:
    key = (settings.openai_api_key or "").strip() or "EMPTY"
    return f"Bearer {key}"


def _use_chat_completions() -> bool:
    """Chat Completions for Aliyun compatible-mode and local llama-cpp-python."""
    base = _base_url().lower()
    return (
        "compatible-mode" in base
        or "127.0.0.1" in base
        or "localhost" in base
    )


def _extract_output_text(data: dict[str, Any]) -> str:
    """Pull assistant text from a Responses API payload."""
    if isinstance(data.get("output_text"), str) and data["output_text"].strip():
        return str(data["output_text"]).strip()

    texts: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") and item.get("type") not in {"message", "output_message"}:
            # Skip reasoning / tool items unless they carry content text.
            if item.get("type") not in {"message", "output_message"} and "content" not in item:
                continue
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            texts.append(content.strip())
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                if isinstance(part, str) and part.strip():
                    texts.append(part.strip())
                continue
            ptype = str(part.get("type") or "")
            if ptype in {"output_text", "text"} and part.get("text"):
                texts.append(str(part["text"]))
            elif part.get("text"):
                texts.append(str(part["text"]))
    if texts:
        return "\n".join(texts).strip()

    # Fallbacks seen in some OpenAI-compatible servers
    choices = data.get("choices") or []
    if choices and isinstance(choices[0], dict):
        msg = choices[0].get("message") or {}
        if isinstance(msg, dict) and msg.get("content"):
            c = msg["content"]
            if isinstance(c, list):
                return "".join(
                    str(p.get("text") if isinstance(p, dict) else p) for p in c
                ).strip()
            return str(c).strip()
    return ""


async def _retry_call(
    factory: Callable[[], Any],
    *,
    timeout_s: float,
    attempts: int,
    label: str,
) -> Any:
    def _before_sleep(retry_state) -> None:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        msg = str(exc or "").replace("{", "{{").replace("}", "}}")
        logger.warning(
            "{} retry {}/{} after {:.1f}s: {}",
            label,
            retry_state.attempt_number,
            attempts,
            float(getattr(retry_state.next_action, "sleep", 0) or 0),
            msg[:300],
        )

    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(attempts),
        wait=wait_random_exponential(multiplier=2.0, min=6, max=90),
        retry=retry_if_exception(_is_retryable),
        before_sleep=_before_sleep,
        reraise=True,
    ):
        with attempt:
            await asyncio.sleep(random.uniform(0.05, 0.35))
            return await asyncio.wait_for(factory(), timeout=timeout_s)
    raise RuntimeError(f"{label} unreachable")


class OpenAIResponsesClient:
    async def _post_json(self, path: str, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        url = f"{_base_url()}{path}"
        headers = {
            "Authorization": _auth_header(),
            "Content-Type": "application/json",
        }
        # Long read timeout: vLLM buffers the full response; tunnel must stay
        # alive during multi-minute generation (see hardened tunnel keepalive).
        timeout = httpx.Timeout(
            connect=30.0,
            read=float(timeout_s),
            write=120.0,
            pool=30.0,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code == 429:
            await vl_limiter.on_rate_limited()
        if resp.status_code >= 400:
            detail = resp.text[:800]
            try:
                err = resp.json()
                if isinstance(err, dict):
                    detail = str(err.get("error") or err.get("message") or detail)
            except Exception:
                pass
            raise OpenAIResponsesAPIError(
                f"{path} failed: status={resp.status_code} {detail}",
                status_code=resp.status_code,
            )
        data = resp.json()
        if not isinstance(data, dict):
            raise OpenAIResponsesAPIError(f"{path} returned non-object JSON")
        return data

    async def _post_responses(self, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        return await self._post_json("/responses", payload, timeout_s=timeout_s)

    async def _post_chat_completions(
        self, payload: dict[str, Any], *, timeout_s: float
    ) -> dict[str, Any]:
        return await self._post_json("/chat/completions", payload, timeout_s=timeout_s)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        model_name = model or active_chat_model()

        if _use_chat_completions():
            chat_messages: list[dict[str, Any]] = []
            for msg in messages:
                role = str(msg.get("role") or "user")
                content = msg.get("content")
                text = content if isinstance(content, str) else str(content or "")
                chat_messages.append({"role": role, "content": text})
            payload: dict[str, Any] = {
                "model": model_name,
                "messages": chat_messages,
            }
            if "compatible-mode" in _base_url().lower():
                payload["enable_thinking"] = False
            if temperature is not None:
                payload["temperature"] = temperature

            async def _do_chat_cc() -> dict[str, Any]:
                t0 = time.perf_counter()
                ok = True
                err = None
                try:
                    async with vl_limiter.acquire():
                        data = await self._post_chat_completions(
                            payload, timeout_s=_CHAT_CALL_TIMEOUT_S
                        )
                    text = _extract_output_text(data)
                    return {
                        "choices": [
                            {"index": 0, "message": {"role": "assistant", "content": text}}
                        ],
                    }
                except Exception as exc:
                    ok = False
                    err = str(exc)[:240]
                    raise
                finally:
                    _record_vl_metric(
                        {
                            "ts": time.time(),
                            "kind": "chat",
                            "model": model_name,
                            "duration_s": round(time.perf_counter() - t0, 3),
                            "ok": ok,
                            "error": err,
                        }
                    )

            return await _retry_call(
                _do_chat_cc,
                timeout_s=_CHAT_CALL_TIMEOUT_S,
                attempts=6,
                label="openai_chat.chat",
            )

        instructions = ""
        input_items: list[dict[str, Any]] = []
        for msg in messages:
            role = str(msg.get("role") or "user")
            content = msg.get("content")
            text = content if isinstance(content, str) else str(content or "")
            if role == "system":
                instructions = f"{instructions}\n{text}".strip() if instructions else text
                continue
            input_items.append(
                {
                    "type": "message",
                    "role": "user" if role == "user" else role,
                    "content": [{"type": "input_text", "text": text}],
                }
            )
        if not input_items:
            input_items = [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": instructions or ""}],
                }
            ]
            instructions = ""

        payload = {
            "model": model_name,
            "input": input_items,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if instructions:
            payload["instructions"] = instructions
        if temperature is not None:
            payload["temperature"] = temperature

        async def _do_call() -> dict[str, Any]:
            t0 = time.perf_counter()
            ok = True
            err = None
            try:
                async with vl_limiter.acquire():
                    data = await self._post_responses(payload, timeout_s=_CHAT_CALL_TIMEOUT_S)
                text = _extract_output_text(data)
                return {
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
                }
            except Exception as exc:
                ok = False
                err = str(exc)[:240]
                raise
            finally:
                _record_vl_metric(
                    {
                        "ts": time.time(),
                        "kind": "chat",
                        "model": model_name,
                        "duration_s": round(time.perf_counter() - t0, 3),
                        "ok": ok,
                        "error": err,
                    }
                )

        return await _retry_call(
            _do_call,
            timeout_s=_CHAT_CALL_TIMEOUT_S,
            attempts=6,
            label="openai_responses.chat",
        )

    async def extract_page_layout(
        self,
        path: str | Path,
        *,
        page: int = 1,
        extra_hint: str | None = None,
    ) -> str:
        p = Path(path).resolve()
        if not p.is_file():
            raise FileNotFoundError(str(p))

        mime, _ = mimetypes.guess_type(p.name)
        if not mime or not mime.startswith("image/"):
            mime = "image/png"
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        image_ref = f"data:{mime};base64,{b64}"
        prompt = (
            "/no_think\n"
            f"这是扫描文献 PDF 的第 {page} 页图像。"
            "请做版面分析与 OCR，只输出 JSON（不要 Markdown 解释），格式：\n"
            '{"blocks":[{"type":"text|sectionheader|pagefooter|pageheader",'
            '"text":"...","bbox":[x0,y0,x1,y1]}]}\n'
            "坐标 bbox 使用 0–1000 归一化（相对页宽高）。"
            "每个块必须给出紧贴文字的 bbox，禁止用整页 [0,0,1000,1000]。"
            "标题、作者、小节标题、正文段落、页码要分成多个块，各自独立 bbox。"
            "页眉页脚用 pageheader/pagefooter；正文用 text；标题用 sectionheader。"
            "尽量完整保留文字，勿编造。"
            "文字一律使用简体中文；若原文为繁体，请转换为简体后再写入 text 字段。"
            "不要输出思考过程，直接给出 JSON。"
            f"{STRICT_BBOX_HINT}"
        )
        if extra_hint:
            prompt = prompt + str(extra_hint)

        if _use_chat_completions():
            cc_payload: dict[str, Any] = {
                "model": active_vision_model(),
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": image_ref}},
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            }
            if "compatible-mode" in _base_url().lower():
                cc_payload["enable_thinking"] = False

            async def _do_cc() -> str:
                t0 = time.perf_counter()
                ok = True
                err = None
                out_chars = 0
                try:
                    async with vl_limiter.acquire():
                        data = await self._post_chat_completions(
                            cc_payload, timeout_s=_VL_CALL_TIMEOUT_S
                        )
                    text = _extract_output_text(data)
                    if not text:
                        raise OpenAIResponsesAPIError(
                            "extract_page_layout returned empty content"
                        )
                    out_chars = len(text)
                    return text
                except Exception as exc:
                    ok = False
                    err = str(exc)[:240]
                    raise
                finally:
                    _record_vl_metric(
                        {
                            "ts": time.time(),
                            "kind": "vision_ocr",
                            "model": active_vision_model(),
                            "page": page,
                            "image": p.name,
                            "duration_s": round(time.perf_counter() - t0, 3),
                            "out_chars": out_chars,
                            "ok": ok,
                            "error": err,
                        }
                    )

            return await _retry_call(
                _do_cc,
                timeout_s=_VL_CALL_TIMEOUT_S,
                attempts=6,
                label=f"openai_chat.extract_page_layout page={page}",
            )

        payload: dict[str, Any] = {
            "model": active_vision_model(),
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": image_ref,
                            "detail": "auto",
                        },
                        {"type": "input_text", "text": prompt},
                    ],
                }
            ],
            # No output token cap. Disable Qwen thinking so generation stays usable
            # over the SSH tunnel (idle ~66s cut still applies if a call runs too long).
            "chat_template_kwargs": {"enable_thinking": False},
        }

        async def _do_call() -> str:
            t0 = time.perf_counter()
            ok = True
            err = None
            out_chars = 0
            try:
                async with vl_limiter.acquire():
                    data = await self._post_responses(payload, timeout_s=_VL_CALL_TIMEOUT_S)
                text = _extract_output_text(data)
                if not text:
                    raise OpenAIResponsesAPIError("extract_page_layout returned empty content")
                out_chars = len(text)
                return text
            except Exception as exc:
                ok = False
                err = str(exc)[:240]
                raise
            finally:
                _record_vl_metric(
                    {
                        "ts": time.time(),
                        "kind": "vision_ocr",
                        "model": active_vision_model(),
                        "page": page,
                        "image": p.name,
                        "duration_s": round(time.perf_counter() - t0, 3),
                        "out_chars": out_chars,
                        "ok": ok,
                        "error": err,
                    }
                )

        return await _retry_call(
            _do_call,
            timeout_s=_VL_CALL_TIMEOUT_S,
            attempts=6,
            label=f"openai_responses.extract_page_layout page={page}",
        )


openai_responses_client = OpenAIResponsesClient()
