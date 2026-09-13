"""Select the active VL / chat client based on llm_provider."""
from __future__ import annotations

from typing import Any, Protocol

from ocr_app.config import (
    active_chat_model,
    active_llm_provider,
    active_vision_model,
    settings,
)
from ocr_app.ocr_core.dashscope_vl import dashscope_client
from ocr_app.ocr_core.openai_responses import openai_responses_client

# Re-export helpers for callers that import from this module.
__all__ = [
    "VLClient",
    "active_chat_model",
    "active_provider",
    "active_vision_model",
    "get_vl_client",
    "require_llm_configured",
]


class VLClient(Protocol):
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]: ...

    async def extract_page_layout(
        self,
        path: str | Any,
        *,
        page: int = 1,
        extra_hint: str | None = None,
    ) -> str: ...


def active_provider() -> str:
    return active_llm_provider()


def get_vl_client() -> VLClient:
    if active_provider() == "openai_responses":
        return openai_responses_client
    return dashscope_client


def require_llm_configured() -> str | None:
    """Return an error message if the active provider is not configured, else None."""
    provider = active_provider()
    if provider == "openai_responses":
        base = (settings.openai_base_url or "").strip()
        if not base:
            return "OPENAI_BASE_URL not configured"
        if not active_vision_model():
            return "OPENAI_VISION_MODEL not configured"
        return None
    if not (settings.dashscope_api_key or "").strip():
        return "DASHSCOPE_API_KEY not configured"
    return None
