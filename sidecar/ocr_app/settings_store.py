"""Persist settings / secrets under the library data root (portable)."""
from __future__ import annotations

import json
from pathlib import Path

from ocr_app.config import Settings, default_data_root, get_settings, settings


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def settings_file() -> Path:
    return settings.data_root / "settings.json"


def secrets_file() -> Path:
    return settings.data_root / "secrets.json"


def load_persisted_settings() -> dict:
    return _read_json(settings_file())


def save_persisted_settings(data: dict) -> None:
    _write_json(settings_file(), data)


def load_secrets() -> dict:
    return _read_json(secrets_file())


def save_secrets(data: dict) -> None:
    _write_json(secrets_file(), data)


def _write_settings_to(root: Path, data: dict) -> None:
    _write_json(root / "settings.json", data)


def _write_secrets_to(root: Path, data: dict) -> None:
    _write_json(root / "secrets.json", data)


def _load_settings_from(root: Path) -> dict:
    return _read_json(root / "settings.json")


def _load_secrets_from(root: Path) -> dict:
    return _read_json(root / "secrets.json")


def apply_runtime_config(updates: dict) -> Settings:
    """Update env-backed settings and refresh cache.

    Writes authoritative settings+secrets into the library data_root (portable),
    and keeps a data_root pointer under LOCALAPPDATA/OcrLibrary for cold start.
    """
    import os

    key_map = {
        "data_root": "OCR_DATA_ROOT",
        "llm_provider": "LLM_PROVIDER",
        "dashscope_api_key": "DASHSCOPE_API_KEY",
        "dashscope_http_api_url": "DASHSCOPE_HTTP_API_URL",
        "openai_base_url": "OPENAI_BASE_URL",
        "vision_model": "VISION_MODEL",
        "chat_model": "CHAT_MODEL",
        "openai_vision_model": "OPENAI_VISION_MODEL",
        "openai_chat_model": "OPENAI_CHAT_MODEL",
        "vision_pdf_dpi": "VISION_PDF_DPI",
        "vision_pdf_max_pages": "VISION_PDF_MAX_PAGES",
        "vision_review_threshold": "VISION_REVIEW_THRESHOLD",
        "keep_page_images": "OCR_KEEP_PAGE_IMAGES",
        "ocr_page_concurrency": "OCR_PAGE_CONCURRENCY",
        "ocr_document_concurrency": "OCR_DOCUMENT_CONCURRENCY",
        "ocr_api_concurrency": "OCR_API_CONCURRENCY",
        "ocr_worker_processes": "OCR_WORKER_PROCESSES",
        "delivery_submitter": "DELIVERY_SUBMITTER",
    }

    old_root = Path(settings.data_root)
    secrets: dict = {**_load_secrets_from(old_root)}
    persisted: dict = {}

    for k, v in updates.items():
        if k == "dashscope_api_key" and v:
            secrets["dashscope_api_key"] = str(v)
            os.environ["DASHSCOPE_API_KEY"] = str(v)
        elif k == "openai_api_key" and v is not None:
            # Allow explicit empty string for local vLLM (Bearer EMPTY).
            secrets["openai_api_key"] = str(v)
            os.environ["OPENAI_API_KEY"] = str(v)
        elif k in key_map:
            env = key_map[k]
            if k == "data_root":
                persisted["data_root"] = str(Path(str(v)))
                os.environ[env] = str(Path(str(v)))
            elif k == "keep_page_images":
                persisted[k] = bool(v)
                os.environ[env] = "true" if v else "false"
            elif k == "ocr_worker_processes":
                try:
                    n = max(1, min(16, int(v)))
                except (TypeError, ValueError):
                    n = 1
                persisted[k] = n
                os.environ[env] = str(n)
            else:
                persisted[k] = v
                os.environ[env] = str(v)

    new_root = Path(os.environ.get("OCR_DATA_ROOT") or old_root)
    # Merge on-disk library settings (prefer new root if it already has a library).
    base_settings = _load_settings_from(new_root) or _load_settings_from(old_root)
    merged = {**base_settings, **persisted}
    # Always record absolute data_root inside the library folder itself.
    merged["data_root"] = str(new_root.resolve())

    # If switching roots, bring API keys along when the destination has none.
    if new_root.resolve() != old_root.resolve():
        dest_secrets = _load_secrets_from(new_root)
        if not dest_secrets.get("dashscope_api_key") and secrets.get("dashscope_api_key"):
            dest_secrets = {**dest_secrets, "dashscope_api_key": secrets["dashscope_api_key"]}
        if "openai_api_key" not in dest_secrets and "openai_api_key" in secrets:
            dest_secrets = {**dest_secrets, "openai_api_key": secrets["openai_api_key"]}
        secrets = dest_secrets or secrets

    new_root.mkdir(parents=True, exist_ok=True)
    _write_settings_to(new_root, merged)
    if secrets:
        _write_secrets_to(new_root, secrets)

    # Cold-start pointer under default root (LOCALAPPDATA) so next boot finds the library.
    pointer_root = default_data_root()
    if pointer_root.resolve() != new_root.resolve():
        pointer = {**_load_settings_from(pointer_root), "data_root": str(new_root.resolve())}
        _write_settings_to(pointer_root, pointer)

    get_settings.cache_clear()
    resolved = get_settings()
    resolved.data_root.mkdir(parents=True, exist_ok=True)
    return resolved


def bootstrap_from_disk() -> None:
    """Load settings from disk into env.

    First read the default data-root settings (often LOCALAPPDATA) for a
    data_root pointer, then re-read from the resolved data_root so fields
    like dashscope_http_api_url saved under D:\\Library are applied.
    """
    import os

    def _apply(persisted: dict, *, secrets: dict | None = None) -> None:
        if persisted.get("data_root"):
            os.environ["OCR_DATA_ROOT"] = str(persisted["data_root"])
        sec = secrets if secrets is not None else {}
        if sec.get("dashscope_api_key"):
            # Prefer library secrets; only fill if unset.
            os.environ.setdefault("DASHSCOPE_API_KEY", sec["dashscope_api_key"])
        if "openai_api_key" in sec:
            os.environ.setdefault("OPENAI_API_KEY", str(sec.get("openai_api_key") or ""))
        for k in (
            "llm_provider",
            "dashscope_http_api_url",
            "openai_base_url",
            "vision_model",
            "chat_model",
            "openai_vision_model",
            "openai_chat_model",
            "vision_pdf_dpi",
            "vision_pdf_max_pages",
            "ocr_page_concurrency",
            "ocr_document_concurrency",
            "ocr_api_concurrency",
            "ocr_worker_processes",
            "delivery_submitter",
        ):
            if persisted.get(k) is not None:
                env = {
                    "llm_provider": "LLM_PROVIDER",
                    "dashscope_http_api_url": "DASHSCOPE_HTTP_API_URL",
                    "openai_base_url": "OPENAI_BASE_URL",
                    "vision_model": "VISION_MODEL",
                    "chat_model": "CHAT_MODEL",
                    "openai_vision_model": "OPENAI_VISION_MODEL",
                    "openai_chat_model": "OPENAI_CHAT_MODEL",
                    "vision_pdf_dpi": "VISION_PDF_DPI",
                    "vision_pdf_max_pages": "VISION_PDF_MAX_PAGES",
                    "ocr_page_concurrency": "OCR_PAGE_CONCURRENCY",
                    "ocr_document_concurrency": "OCR_DOCUMENT_CONCURRENCY",
                    "ocr_api_concurrency": "OCR_API_CONCURRENCY",
                    "ocr_worker_processes": "OCR_WORKER_PROCESSES",
                    "delivery_submitter": "DELIVERY_SUBMITTER",
                }[k]
                if k == "ocr_worker_processes":
                    try:
                        n = max(1, min(16, int(persisted[k])))
                    except (TypeError, ValueError):
                        n = 1
                    os.environ[env] = str(n)
                else:
                    os.environ[env] = str(persisted[k])

    pointer_root = default_data_root()

    # Pass 1: default root (LOCALAPPDATA) — may only contain data_root redirect
    get_settings.cache_clear()
    first = _load_settings_from(pointer_root)
    first_secrets = _load_secrets_from(pointer_root)
    _apply(first, secrets=first_secrets)
    get_settings.cache_clear()

    # Pass 2: resolved data_root (e.g. D:\Library) — authoritative settings
    library_root = Path(os.environ.get("OCR_DATA_ROOT") or pointer_root)
    second = _load_settings_from(library_root)
    second_secrets = _load_secrets_from(library_root)
    if second or second_secrets:
        # Library secrets override pointer-only secrets when present.
        if second_secrets.get("dashscope_api_key"):
            os.environ["DASHSCOPE_API_KEY"] = str(second_secrets["dashscope_api_key"])
        if "openai_api_key" in second_secrets:
            os.environ["OPENAI_API_KEY"] = str(second_secrets.get("openai_api_key") or "")
        _apply(second, secrets=second_secrets or first_secrets)
        get_settings.cache_clear()

    # Migrate API keys into the portable library folder when missing there.
    resolved_root = Path(get_settings().data_root)
    lib_secrets = _load_secrets_from(resolved_root)
    changed = False
    if not lib_secrets.get("dashscope_api_key"):
        key = os.environ.get("DASHSCOPE_API_KEY") or first_secrets.get("dashscope_api_key")
        if key:
            lib_secrets = {**lib_secrets, "dashscope_api_key": str(key)}
            changed = True
    if "openai_api_key" not in lib_secrets:
        okey = os.environ.get("OPENAI_API_KEY")
        if okey is None and "openai_api_key" in first_secrets:
            okey = first_secrets.get("openai_api_key")
        if okey is not None:
            lib_secrets = {**lib_secrets, "openai_api_key": str(okey)}
            changed = True
    if changed:
        _write_secrets_to(resolved_root, lib_secrets)

    # Keep cold-start pointer in sync with the active library.
    if resolved_root.resolve() != pointer_root.resolve():
        pointer = {**_load_settings_from(pointer_root), "data_root": str(resolved_root.resolve())}
        _write_settings_to(pointer_root, pointer)
