from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path

from ocr_app.config import settings


def doc_dir(document_id: str) -> Path:
    return settings.data_root / "docs" / document_id


def doc_relative_pdf(document_id: str) -> str:
    return f"docs/{document_id}/source.pdf"


def doc_relative_thumb(document_id: str) -> str:
    return f"docs/{document_id}/thumb.png"


def abs_from_relative(rel: str) -> Path:
    return settings.data_root / rel.replace("\\", "/")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def new_id() -> str:
    return str(uuid.uuid4())


def metadata_json(data: dict | None) -> str | None:
    if not data:
        return None
    return json.dumps(data, ensure_ascii=False)


def parse_metadata(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def copy_pdf(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


def remove_doc_tree(document_id: str) -> None:
    d = doc_dir(document_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
