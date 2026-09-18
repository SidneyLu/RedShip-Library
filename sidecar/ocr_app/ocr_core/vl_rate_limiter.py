"""Global adaptive rate limiter for DashScope VL / chat API calls."""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

from loguru import logger

from ocr_app.config import settings

_COOLDOWN_S = 30.0
_TRANSIENT_COOLDOWN_S = 8.0
# Recover concurrency slowly so we don't stampede the gateway after a drop.
_RECOVERY_EVERY = 20


class VLLimiter(Protocol):
    """Process-local or cross-process VL/chat concurrency gate."""

    def acquire(self) -> AsyncIterator[None]: ...

    async def on_rate_limited(self) -> None: ...

    async def on_transient_failure(self) -> None: ...


class AdaptiveVLLimiter:
    """Caps in-flight VL calls globally; halves limit on 429 / disconnects."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._cond = asyncio.Condition(self._lock)
        self._configured_max = 128
        self._effective_limit = 128
        self._in_flight = 0
        self._cooldown_until = 0.0
        self._success_since_recovery = 0

    def _sync_from_settings(self) -> None:
        configured = max(1, int(settings.ocr_api_concurrency))
        if configured != self._configured_max:
            self._configured_max = configured
            if self._effective_limit > configured:
                self._effective_limit = configured
            elif self._in_flight == 0 and self._effective_limit < configured:
                self._effective_limit = configured

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        self._sync_from_settings()
        while True:
            async with self._lock:
                now = time.monotonic()
                if now < self._cooldown_until:
                    wait_s = self._cooldown_until - now
                elif self._in_flight < self._effective_limit:
                    self._in_flight += 1
                    break
                else:
                    wait_s = None
            if wait_s is not None:
                await asyncio.sleep(min(wait_s, 0.5))
                continue
            async with self._cond:
                await self._cond.wait()

        try:
            yield
        finally:
            async with self._lock:
                self._in_flight = max(0, self._in_flight - 1)
                self._success_since_recovery += 1
                if (
                    self._success_since_recovery >= _RECOVERY_EVERY
                    and self._effective_limit < self._configured_max
                ):
                    self._effective_limit = min(
                        self._configured_max, self._effective_limit + 1
                    )
                    self._success_since_recovery = 0
                    logger.info(
                        "VL rate limit recovered to {}/{}",
                        self._effective_limit,
                        self._configured_max,
                    )
            async with self._cond:
                self._cond.notify()

    async def _throttle(self, *, cooldown_s: float, reason: str) -> None:
        async with self._lock:
            prev = self._effective_limit
            self._effective_limit = max(1, self._effective_limit // 2)
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + cooldown_s)
            self._success_since_recovery = 0
            if self._effective_limit < prev:
                logger.warning(
                    "VL {}: reducing concurrency {} -> {} for {:.0f}s",
                    reason,
                    prev,
                    self._effective_limit,
                    cooldown_s,
                )
        async with self._cond:
            self._cond.notify_all()

    async def on_rate_limited(self) -> None:
        await self._throttle(cooldown_s=_COOLDOWN_S, reason="rate limited (429)")

    async def on_transient_failure(self) -> None:
        """Server disconnect / reset — back off so we don't keep ripping sockets."""
        await self._throttle(cooldown_s=_TRANSIENT_COOLDOWN_S, reason="connection drop")


class SharedVLLimiterState:
    """Picklable cross-process counters for MultiprocessVLLimiter."""

    def __init__(self, ctx, *, configured_max: int) -> None:
        self.lock = ctx.RLock()
        self.configured_max = ctx.Value("i", max(1, configured_max))
        self.effective_limit = ctx.Value("i", max(1, configured_max))
        self.in_flight = ctx.Value("i", 0)
        self.cooldown_until = ctx.Value("d", 0.0)
        self.success_since_recovery = ctx.Value("i", 0)

    def reconfigure(self, configured_max: int) -> None:
        configured = max(1, configured_max)
        with self.lock:
            prev = self.configured_max.value
            self.configured_max.value = configured
            if self.effective_limit.value > configured:
                self.effective_limit.value = configured
            elif self.in_flight.value == 0 and self.effective_limit.value < configured:
                self.effective_limit.value = configured
            if prev != configured:
                logger.info(
                    "Shared VL limiter reconfigured {} -> {}",
                    prev,
                    configured,
                )


class MultiprocessVLLimiter:
    """Same adaptive policy as AdaptiveVLLimiter, backed by shared memory."""

    def __init__(self, shared: SharedVLLimiterState) -> None:
        self._shared = shared

    def _try_acquire(self) -> float | None:
        """Return None if acquired, else seconds to wait (0 = contended)."""
        with self._shared.lock:
            now = time.time()
            cooldown = self._shared.cooldown_until.value
            if now < cooldown:
                return cooldown - now
            if self._shared.in_flight.value < self._shared.effective_limit.value:
                self._shared.in_flight.value += 1
                return None
            return 0.0

    def _release(self) -> None:
        with self._shared.lock:
            self._shared.in_flight.value = max(0, self._shared.in_flight.value - 1)
            self._shared.success_since_recovery.value += 1
            if (
                self._shared.success_since_recovery.value >= _RECOVERY_EVERY
                and self._shared.effective_limit.value < self._shared.configured_max.value
            ):
                self._shared.effective_limit.value = min(
                    self._shared.configured_max.value,
                    self._shared.effective_limit.value + 1,
                )
                self._shared.success_since_recovery.value = 0
                logger.info(
                    "VL rate limit recovered to {}/{}",
                    self._shared.effective_limit.value,
                    self._shared.configured_max.value,
                )

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        while True:
            wait_s = await asyncio.to_thread(self._try_acquire)
            if wait_s is None:
                break
            if wait_s > 0:
                await asyncio.sleep(min(wait_s, 0.5))
            else:
                await asyncio.sleep(0.05)

        try:
            yield
        finally:
            await asyncio.to_thread(self._release)

    def _throttle_sync(self, *, cooldown_s: float, reason: str) -> None:
        with self._shared.lock:
            prev = self._shared.effective_limit.value
            self._shared.effective_limit.value = max(1, prev // 2)
            self._shared.cooldown_until.value = max(
                self._shared.cooldown_until.value, time.time() + cooldown_s
            )
            self._shared.success_since_recovery.value = 0
            if self._shared.effective_limit.value < prev:
                logger.warning(
                    "VL {}: reducing concurrency {} -> {} for {:.0f}s",
                    reason,
                    prev,
                    self._shared.effective_limit.value,
                    cooldown_s,
                )

    async def on_rate_limited(self) -> None:
        await asyncio.to_thread(
            self._throttle_sync, cooldown_s=_COOLDOWN_S, reason="rate limited (429)"
        )

    async def on_transient_failure(self) -> None:
        await asyncio.to_thread(
            self._throttle_sync,
            cooldown_s=_TRANSIENT_COOLDOWN_S,
            reason="connection drop",
        )


class _VLLimiterProxy:
    """Stable import target; delegates to the active process limiter."""

    def acquire(self):
        return _active.acquire()

    async def on_rate_limited(self) -> None:
        await _active.on_rate_limited()

    async def on_transient_failure(self) -> None:
        await _active.on_transient_failure()


_active: AdaptiveVLLimiter | MultiprocessVLLimiter = AdaptiveVLLimiter()
vl_limiter = _VLLimiterProxy()


def get_vl_limiter() -> AdaptiveVLLimiter | MultiprocessVLLimiter:
    return _active


def set_vl_limiter(limiter: AdaptiveVLLimiter | MultiprocessVLLimiter) -> None:
    global _active
    _active = limiter
    logger.info("VL limiter installed: {}", type(limiter).__name__)
