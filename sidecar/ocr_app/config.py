"""Runtime settings for OCR sidecar."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("HOME") or "."
    return Path(base) / "OcrLibrary"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    data_root: Path = Field(default_factory=default_data_root, alias="OCR_DATA_ROOT")
    llm_provider: str = Field(default="dashscope", alias="LLM_PROVIDER")
    dashscope_api_key: str = Field(default="", alias="DASHSCOPE_API_KEY")
    dashscope_http_api_url: str = Field(
        default="https://dashscope.aliyuncs.com/api/v1",
        alias="DASHSCOPE_HTTP_API_URL",
        validation_alias=AliasChoices("DASHSCOPE_HTTP_API_URL", "DASHSCOPE_BASE_URL"),
    )
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_base_url: str = Field(
        default="http://127.0.0.1:8000/v1",
        alias="OPENAI_BASE_URL",
    )
    # DashScope models
    vision_model: str = Field(default="qwen3.5-flash", alias="VISION_MODEL")
    chat_model: str = Field(default="qwen3.5-flash", alias="CHAT_MODEL")
    # OpenAI Responses / vLLM models (independent of DashScope)
    openai_vision_model: str = Field(default="", alias="OPENAI_VISION_MODEL")
    openai_chat_model: str = Field(default="", alias="OPENAI_CHAT_MODEL")
    vision_pdf_dpi: int = Field(default=300, alias="VISION_PDF_DPI")
    vision_pdf_max_pages: int = Field(default=10000, alias="VISION_PDF_MAX_PAGES")
    vision_review_threshold: float = Field(default=0.6, alias="VISION_REVIEW_THRESHOLD")
    keep_page_images: bool = Field(default=False, alias="OCR_KEEP_PAGE_IMAGES")
    ocr_page_concurrency: int = Field(default=16, alias="OCR_PAGE_CONCURRENCY")
    ocr_document_concurrency: int = Field(default=8, alias="OCR_DOCUMENT_CONCURRENCY")
    ocr_api_concurrency: int = Field(default=128, alias="OCR_API_CONCURRENCY")
    # 1 = in-process OCR (default). 2+ = Coordinator + Worker processes.
    ocr_worker_processes: int = Field(default=1, alias="OCR_WORKER_PROCESSES")
    delivery_submitter: str = Field(default="", alias="DELIVERY_SUBMITTER")
    host: str = Field(default="127.0.0.1", alias="OCR_HOST")
    port: int = Field(default=18765, alias="OCR_PORT")

    @field_validator("ocr_worker_processes", mode="before")
    @classmethod
    def _clamp_worker_processes(cls, v: object) -> int:
        try:
            n = int(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 1
        return max(1, min(16, n))



@lru_cache
def get_settings() -> Settings:
    return Settings()


class _SettingsProxy:
    """Delegate to get_settings() so runtime env / secrets updates take effect."""

    def __getattr__(self, name: str):
        return getattr(get_settings(), name)

    def __repr__(self) -> str:
        return repr(get_settings())


settings: _SettingsProxy = _SettingsProxy()


def active_llm_provider() -> str:
    return (settings.llm_provider or "dashscope").strip().lower()


def active_vision_model() -> str:
    if active_llm_provider() == "openai_responses":
        return (settings.openai_vision_model or settings.vision_model or "").strip()
    return (settings.vision_model or "").strip()


def active_chat_model() -> str:
    if active_llm_provider() == "openai_responses":
        return (settings.openai_chat_model or settings.chat_model or "").strip()
    return (settings.chat_model or "").strip()
