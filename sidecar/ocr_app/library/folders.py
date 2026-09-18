"""Virtual folders backed by Document.series + folders.json registry."""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ocr_app.config import settings
from ocr_app.db.models import Document


def folders_file() -> Path:
    return settings.data_root / "folders.json"


def load_folder_names() -> list[str]:
    p = folders_file()
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("folders"), list):
            names = [str(x).strip() for x in data["folders"] if str(x).strip()]
            # preserve order, unique
            seen: set[str] = set()
            out: list[str] = []
            for n in names:
                if n not in seen:
                    seen.add(n)
                    out.append(n)
            return out
    except (json.JSONDecodeError, OSError):
        pass
    return []


def save_folder_names(names: list[str]) -> None:
    settings.data_root.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    cleaned: list[str] = []
    for n in names:
        name = str(n).strip()
        if name and name not in seen:
            seen.add(name)
            cleaned.append(name)
    folders_file().write_text(
        json.dumps({"folders": cleaned}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def ensure_folder_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError("folder name required")
    names = load_folder_names()
    if name not in names:
        names.append(name)
        save_folder_names(names)
    return name


async def list_folders(session: AsyncSession) -> dict:
    """Merge registered folders with series values in use; include counts."""
    rows = (
        await session.execute(
            select(Document.series, func.count())
            .where(Document.series.is_not(None), Document.series != "")
            .group_by(Document.series)
        )
    ).all()
    counts: dict[str, int] = {str(name): int(c) for name, c in rows if name}
    registered = load_folder_names()
    for name in counts:
        if name not in registered:
            registered.append(name)
    if counts:
        # persist any series discovered from docs
        save_folder_names(registered)

    uncategorized = await session.scalar(
        select(func.count())
        .select_from(Document)
        .where((Document.series.is_(None)) | (Document.series == ""))
    )
    total = await session.scalar(select(func.count()).select_from(Document))

    items = [
        {"name": name, "count": counts.get(name, 0)}
        for name in registered
    ]
    return {
        "folders": items,
        "uncategorized_count": int(uncategorized or 0),
        "total": int(total or 0),
    }


async def rename_folder(session: AsyncSession, old_name: str, new_name: str) -> dict:
    old_name = old_name.strip()
    new_name = new_name.strip()
    if not old_name or not new_name:
        raise ValueError("folder name required")
    if old_name == new_name:
        return await list_folders(session)

    names = load_folder_names()
    if new_name in names and new_name != old_name:
        raise ValueError(f"folder already exists: {new_name}")

    await session.execute(
        update(Document).where(Document.series == old_name).values(series=new_name)
    )
    await session.commit()

    names = [new_name if n == old_name else n for n in names]
    if new_name not in names:
        names.append(new_name)
    save_folder_names(names)
    return await list_folders(session)


async def delete_folder(session: AsyncSession, name: str, *, move_to: str | None = None) -> dict:
    """Remove folder from registry; reassign docs to move_to or uncategorized."""
    name = name.strip()
    if not name:
        raise ValueError("folder name required")
    target = move_to.strip() if move_to and move_to.strip() else None
    if target:
        ensure_folder_name(target)

    await session.execute(
        update(Document)
        .where(Document.series == name)
        .values(series=target)
    )
    await session.commit()

    names = [n for n in load_folder_names() if n != name]
    save_folder_names(names)
    return await list_folders(session)


async def move_documents(
    session: AsyncSession,
    doc_ids: list[str],
    *,
    folder: str | None,
) -> dict:
    """Assign documents to a folder (or uncategorized if folder is None/empty)."""
    ids = [str(x).strip() for x in doc_ids if str(x).strip()]
    if not ids:
        raise ValueError("document ids required")

    target: str | None = None
    if folder is not None and str(folder).strip():
        target = ensure_folder_name(str(folder).strip())

    await session.execute(
        update(Document).where(Document.id.in_(ids)).values(series=target)
    )
    await session.commit()
    report = await list_folders(session)
    return {
        "moved": len(ids),
        "folder": target,
        **report,
    }
