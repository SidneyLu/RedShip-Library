from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ocr_app.config import settings
from ocr_app.db.models import Base

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_engine_url: str | None = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None
    return _session_factory


def _db_url() -> str:
    db_path = settings.data_root / "library.db"
    return f"sqlite+aiosqlite:///{db_path.as_posix()}"


def _configure_sqlite(dbapi_conn, _connection_record) -> None:
    """Enable WAL and wait on locks — required for multi-document OCR."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=60000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def init_engine(*, force: bool = False) -> None:
    """Create engine once; recreate only when data_root changes or force=True.

    Calling this on every OCR job used to spawn multiple engines against the same
    SQLite file and effectively serialize / lock concurrent document OCR.
    """
    global _engine, _session_factory, _engine_url

    settings.data_root.mkdir(parents=True, exist_ok=True)
    (settings.data_root / "docs").mkdir(parents=True, exist_ok=True)
    url = _db_url()

    if not force and _engine is not None and _engine_url == url:
        return

    old_engine = _engine
    engine = create_async_engine(
        url,
        echo=False,
        connect_args={"timeout": 60},
    )
    event.listen(engine.sync_engine, "connect", _configure_sqlite)
    _engine = engine
    _engine_url = url
    _session_factory = async_sessionmaker(engine, expire_on_commit=False)

    if old_engine is not None:
        # Best-effort dispose; may already be closed if event loop stopped.
        try:
            import asyncio

            loop = asyncio.get_running_loop()
            loop.create_task(old_engine.dispose())
        except RuntimeError:
            pass


async def create_tables() -> None:
    if _engine is None:
        init_engine()
    assert _engine is not None
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    factory = get_session_factory()
    async with factory() as session:
        yield session
