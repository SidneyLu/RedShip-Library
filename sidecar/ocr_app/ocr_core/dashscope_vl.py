"""Slim DashScope client for VL layout + chat review."""
from __future__ import annotations

import asyncio
import base64
import mimetypes
import random
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable, TypeVar

import dashscope
import httpx
from dashscope import AioGeneration, AioMultiModalConversation
from loguru import logger
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from ocr_app.config import active_chat_model, active_vision_model, settings
from ocr_app.ocr_core.dashscope_http_pool import note_api_success, note_transient_disconnect
from ocr_app.ocr_core.vl_rate_limiter import vl_limiter

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore[assignment]

_AIOHTTP_ERRORS: tuple[type[BaseException], ...] = ()
if aiohttp is not None:
    _AIOHTTP_ERRORS = (
        aiohttp.ClientError,
        aiohttp.ServerDisconnectedError,
        aiohttp.ClientConnectorError,
        aiohttp.ClientOSError,
        aiohttp.ClientPayloadError,
        aiohttp.ClientResponseError,
    )

_RETRYABLE = (
    httpx.HTTPError,
    httpx.RemoteProtocolError,
    httpx.ReadTimeout,
    httpx.ConnectTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    asyncio.TimeoutError,
    TimeoutError,
    ConnectionError,
    OSError,
    BrokenPipeError,
    *_AIOHTTP_ERRORS,
)
_MULTIMODAL_PREFIXES = ("qwen3.5", "qwen3.6", "qwen3.7", "qwen3-5", "qwen3-6", "qwen3-7")
# Align with DashScope DEFAULT_REQUEST_TIMEOUT_SECONDS (300) for large PNG uploads.
_VL_CALL_TIMEOUT_S = 300
_CHAT_CALL_TIMEOUT_S = 120
_RETRY_STATUS = {429, 500, 502, 503, 504}

# Merged into every layout prompt so most pages need only one VL call.
STRICT_BBOX_HINT = (
    "重要：每个文字块必须有独立、紧贴文字的 bbox。"
    "禁止输出整页 [0,0,1000,1000]。标题与正文必须分开。"
)

T = TypeVar("T")


class DashScopeAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code

    @property
    def is_data_inspection(self) -> bool:
        blob = f"{self.code or ''} {self}".lower()
        return "datainspectionfailed" in blob.replace("_", "") or "data_inspection" in blob


def is_data_inspection_error(exc: BaseException) -> bool:
    """True for DashScope content-safety blocks (not worth retrying identical input)."""
    if isinstance(exc, DashScopeAPIError) and exc.is_data_inspection:
        return True
    blob = str(exc).lower().replace("_", "")
    return "datainspectionfailed" in blob or "data inspection failed" in blob.replace(
        "_", " "
    )


def _is_transient_disconnect(exc: BaseException) -> bool:
    if _AIOHTTP_ERRORS and isinstance(exc, _AIOHTTP_ERRORS):
        return True
    if isinstance(
        exc,
        (
            httpx.RemoteProtocolError,
            httpx.ReadError,
            httpx.WriteError,
            httpx.ConnectError,
            ConnectionError,
            BrokenPipeError,
            asyncio.TimeoutError,
            TimeoutError,
        ),
    ):
        return True
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "server disconnected",
            "connection reset",
            "connection aborted",
            "broken pipe",
            "remote protocol",
            "server disconnected without sending",
            "peer closed",
            "errno 10054",
            "errno 104",
            "clientconnectorerror",
            "cannot connect",
        )
    )


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, DashScopeAPIError) and exc.is_data_inspection:
        # Content-safety blocks won't succeed on identical retries.
        return False
    if _is_transient_disconnect(exc):
        return True
    if isinstance(exc, _RETRYABLE):
        return True
    if isinstance(exc, DashScopeAPIError):
        if exc.status_code in _RETRY_STATUS:
            return True
        msg = str(exc).lower()
        return any(
            token in msg
            for token in (
                "disconnect",
                "connection reset",
                "connection aborted",
                "broken pipe",
                "timeout",
                "temporarily",
                "connection",
                "reset",
                "unavailable",
                "too many",
                "rate",
                "throttl",
                "429",
                "502",
                "503",
                "504",
                "server disconnected",
            )
        )
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "disconnect",
            "connection reset",
            "connection aborted",
            "broken pipe",
            "timeout",
            "temporarily",
            "connection",
            "reset",
            "unavailable",
            "429",
            "502",
            "503",
            "504",
            "server disconnected",
        )
    )


def _ensure_sdk() -> None:
    dashscope.api_key = settings.dashscope_api_key
    dashscope.base_http_api_url = settings.dashscope_http_api_url.rstrip("/")


async def _call_with_timeout(factory: Callable[[], Any], *, timeout_s: float) -> Any:
    return await asyncio.wait_for(factory(), timeout=timeout_s)


async def _retry_call(
    factory: Callable[[], Any],
    *,
    timeout_s: float,
    attempts: int,
    label: str,
) -> Any:
    def _before_sleep(retry_state) -> None:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        # loguru treats {} as format fields — escape exception text.
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
        # Higher min backoff reduces reconnect stampedes after disconnects.
        wait=wait_random_exponential(multiplier=2.0, min=6, max=90),
        retry=retry_if_exception(_is_retryable),
        before_sleep=_before_sleep,
        reraise=True,
    ):
        with attempt:
            await asyncio.sleep(random.uniform(0.05, 0.35))
            try:
                result = await _call_with_timeout(factory, timeout_s=timeout_s)
                note_api_success()
                return result
            except Exception as exc:
                if _is_transient_disconnect(exc):
                    await vl_limiter.on_transient_failure()
                    await note_transient_disconnect()
                elif isinstance(exc, DashScopeAPIError) and exc.status_code == 429:
                    await vl_limiter.on_rate_limited()
                raise
    raise RuntimeError(f"{label} unreachable")


def _obj_to_dict(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _obj_to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_obj_to_dict(v) for v in obj]
    if hasattr(obj, "__dict__"):
        raw = {k: v for k, v in vars(obj).items() if not k.startswith("_")}
        if raw:
            return {k: _obj_to_dict(v) for k, v in raw.items()}
    return obj


def _is_multimodal(model: str) -> bool:
    name = (model or "").strip().lower().replace("_", "-")
    return any(name.startswith(p) for p in _MULTIMODAL_PREFIXES)


def _to_mm_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in messages:
        item = dict(msg)
        content = item.get("content")
        if isinstance(content, str):
            item["content"] = [{"text": content}]
        out.append(item)
    return out


def _raise_if_failed(resp: Any, *, what: str) -> None:
    status = getattr(resp, "status_code", None)
    if status is None or int(status) == int(HTTPStatus.OK):
        return
    code = getattr(resp, "code", None)
    message = getattr(resp, "message", None)
    raise DashScopeAPIError(
        f"{what} failed: status={status} code={code} message={message}",
        status_code=int(status) if status is not None else None,
        code=str(code) if code else None,
    )


def _normalize_chat_response(resp: Any) -> dict[str, Any]:
    _raise_if_failed(resp, what="chat")
    data = _obj_to_dict(getattr(resp, "output", None)) or {}
    choices = data.get("choices") or []
    content = ""
    if choices:
        choice = choices[0] if isinstance(choices[0], dict) else _obj_to_dict(choices[0])
        msg = (choice or {}).get("message") or {}
        if not isinstance(msg, dict):
            msg = _obj_to_dict(msg) or {}
        c = msg.get("content") or ""
        if isinstance(c, list):
            content = "".join(
                str(part.get("text") if isinstance(part, dict) else part) for part in c
            )
        else:
            content = str(c)
    else:
        content = str(data.get("text") or "")
    return {
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
    }


class DashScopeVLClient:
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        _ensure_sdk()
        model_name = model or active_chat_model()
        kwargs: dict[str, Any] = {
            "messages": messages,
            "result_format": "message",
            "api_key": settings.dashscope_api_key,
            "model": model_name,
            "request_timeout": _CHAT_CALL_TIMEOUT_S,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        use_mm = _is_multimodal(model_name)
        if use_mm:
            kwargs["messages"] = _to_mm_messages(messages)

        async def _do_call() -> dict[str, Any]:
            async with vl_limiter.acquire():
                if use_mm:
                    resp = await AioMultiModalConversation.call(**kwargs)
                else:
                    resp = await AioGeneration.call(**kwargs)
            status = getattr(resp, "status_code", None)
            if status is not None and int(status) == 429:
                await vl_limiter.on_rate_limited()
            return _normalize_chat_response(resp)

        return await _retry_call(
            _do_call,
            timeout_s=_CHAT_CALL_TIMEOUT_S,
            attempts=6,
            label="chat",
        )

    async def extract_page_layout(
        self,
        path: str | Path,
        *,
        page: int = 1,
        extra_hint: str | None = None,
    ) -> str:
        _ensure_sdk()
        p = Path(path).resolve()
        if not p.is_file():
            raise FileNotFoundError(str(p))

        mime, _ = mimetypes.guess_type(p.name)
        if not mime or not mime.startswith("image/"):
            mime = "image/png"
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        image_ref = f"data:{mime};base64,{b64}"
        prompt = (
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
            f"{STRICT_BBOX_HINT}"
        )
        if extra_hint:
            prompt = prompt + str(extra_hint)
        messages = [
            {
                "role": "user",
                "content": [{"image": image_ref}, {"text": prompt}],
            }
        ]

        async def _do_call() -> str:
            async with vl_limiter.acquire():
                resp = await AioMultiModalConversation.call(
                    api_key=settings.dashscope_api_key,
                    model=active_vision_model(),
                    messages=messages,
                    request_timeout=_VL_CALL_TIMEOUT_S,
                )
            status = getattr(resp, "status_code", None)
            if status is not None and int(status) != HTTPStatus.OK:
                if int(status) == 429:
                    await vl_limiter.on_rate_limited()
                raise DashScopeAPIError(
                    f"extract_page_layout failed: {getattr(resp, 'code', None)} "
                    f"{getattr(resp, 'message', None)}",
                    status_code=int(status) if status is not None else None,
                    code=str(getattr(resp, "code", None) or ""),
                )
            output = getattr(resp, "output", None)
            data = _obj_to_dict(output) or {}
            choices = data.get("choices") or []
            if choices:
                choice0 = choices[0] if isinstance(choices[0], dict) else _obj_to_dict(choices[0])
                msg = (choice0 or {}).get("message") or {}
                if not isinstance(msg, dict):
                    msg = _obj_to_dict(msg) or {}
                content = msg.get("content")
                if isinstance(content, list) and content:
                    texts = []
                    for part in content:
                        if isinstance(part, dict) and part.get("text"):
                            texts.append(str(part["text"]))
                        elif isinstance(part, str):
                            texts.append(part)
                    if texts:
                        return "\n".join(texts).strip()
                if isinstance(content, str) and content.strip():
                    return content.strip()
            text = data.get("text")
            if text:
                return str(text).strip()
            raise DashScopeAPIError("extract_page_layout returned empty content")

        return await _retry_call(
            _do_call,
            timeout_s=_VL_CALL_TIMEOUT_S,
            attempts=6,
            label=f"extract_page_layout page={page}",
        )


dashscope_client = DashScopeVLClient()
