"""Tuned aiohttp connection pool for DashScope SDK shared sessions."""
from __future__ import annotations

import asyncio
import threading
import weakref
from typing import Any

import aiohttp
from loguru import logger

from ocr_app.config import settings

_lock = threading.RLock()
_install_count = 0
_consecutive_disconnects = 0
_DISCONNECT_REBUILD_THRESHOLD = 5


def _aio_session_module() -> Any:
    from dashscope.api_entities import aio_session as mod

    return mod


def _pool_limits(*, worker_count: int | None = None) -> tuple[int, int]:
    api_conc = max(1, int(settings.ocr_api_concurrency))
    workers = max(
        1,
        int(worker_count)
        if worker_count is not None
        else int(settings.ocr_worker_processes),
    )
    if workers > 1:
        # Each worker process gets a slice of the global API budget.
        limit_per_host = max(1, api_conc // workers)
        limit = limit_per_host + 8
        return limit, limit_per_host
    # Cap slightly above configured API concurrency so waiters don't stall on sockets.
    limit = api_conc + 8
    limit_per_host = api_conc
    return limit, limit_per_host


def _make_session(*, worker_count: int | None = None) -> aiohttp.ClientSession:
    mod = _aio_session_module()
    limit, limit_per_host = _pool_limits(worker_count=worker_count)
    connector = aiohttp.TCPConnector(
        ssl=mod.get_ssl_context(),
        limit=limit,
        limit_per_host=limit_per_host,
        keepalive_timeout=60.0,
        enable_cleanup_closed=True,
        ttl_dns_cache=300,
        force_close=False,
    )
    return aiohttp.ClientSession(connector=connector, trust_env=True)


async def install_dashscope_http_pool(
    *, force: bool = False, worker_count: int | None = None
) -> None:
    """Install a tuned shared ClientSession for the current event loop."""
    global _install_count
    mod = _aio_session_module()
    loop = asyncio.get_running_loop()
    limit, limit_per_host = _pool_limits(worker_count=worker_count)

    old: aiohttp.ClientSession | None = None
    with _lock:
        existing = mod._aio_sessions.get(loop)
        if existing is not None and not existing.closed and not force:
            logger.info(
                "DashScope HTTP pool already installed (limit={} per_host={})",
                limit,
                limit_per_host,
            )
            return
        if existing is not None:
            old = existing
            mod._aio_sessions.pop(loop, None)

        session = _make_session(worker_count=worker_count)
        weakref.finalize(session, mod._sync_close_session, id(session))
        mod._aio_sessions[loop] = session
        _install_count += 1

    if old is not None and not old.closed:
        try:
            await old.close()
        except Exception as exc:
            logger.warning("Failed closing previous DashScope HTTP pool: {}", exc)

    workers = max(
        1,
        int(worker_count)
        if worker_count is not None
        else int(settings.ocr_worker_processes),
    )
    logger.info(
        "DashScope HTTP pool installed (limit={} per_host={} keepalive=60s workers={})",
        limit,
        limit_per_host,
        workers,
    )


async def close_dashscope_http_pool() -> None:
    """Close the shared session for the current event loop."""
    mod = _aio_session_module()
    try:
        await mod.close_shared_aio_session()
        logger.info("DashScope HTTP pool closed")
    except Exception as exc:
        logger.warning("DashScope HTTP pool close failed: {}", exc)


async def rebuild_dashscope_http_pool(*, reason: str) -> None:
    """Drop keep-alive sockets and reinstall the pool after repeated disconnects."""
    global _consecutive_disconnects
    logger.warning("Rebuilding DashScope HTTP pool ({})", reason)
    await install_dashscope_http_pool(force=True)
    with _lock:
        _consecutive_disconnects = 0


def note_api_success() -> None:
    global _consecutive_disconnects
    with _lock:
        _consecutive_disconnects = 0


async def note_transient_disconnect() -> None:
    """Track disconnects; rebuild shared session after a streak."""
    global _consecutive_disconnects
    should_rebuild = False
    with _lock:
        _consecutive_disconnects += 1
        if _consecutive_disconnects >= _DISCONNECT_REBUILD_THRESHOLD:
            should_rebuild = True
            _consecutive_disconnects = 0
    if should_rebuild:
        await rebuild_dashscope_http_pool(
            reason=f"{_DISCONNECT_REBUILD_THRESHOLD} consecutive disconnects",
        )
