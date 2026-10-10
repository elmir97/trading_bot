"""Деплой 3, фикс 5: «🔴 Да, закрыть» под тревогой ждёт лок позиции (до
EXEC_OPEN_CLOSE_LOCK_WAIT_SECONDS, по умолчанию 8 с), а фоновые повторы защиты и
цикл, видя ключ close_wanted (TTL = ожидание + 4 с), уступают. На 🔴 10.10
первое «Да, закрыть» отказало: лок держал повтор B.1."""

from __future__ import annotations

import asyncio
import logging
import os
import time

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis

from app.core.config import Settings
from app.core.locks import LockBusyError, RedisLock, close_wanted_key, position_lock_key
from app.execution.opening.recovery import recover_one
from app.trading.enums import OpeningStatus, OrderRole, OrderStatus
from tests.test_opening_alarm import ALWAYS
from tests.test_opening_confirm import _rows, opening_context
from tests.test_opening_faults import _open, _posts

needs_db = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")


@pytest_asyncio.fixture
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    async for c in opening_context(unique_telegram_id()):
        yield c


# --- RedisLock с ожиданием ----------------------------------------------------------------


async def test_lock_waits_until_released() -> None:
    redis = FakeRedis(decode_responses=True)
    holder = RedisLock(redis, "k", 30)
    await holder.__aenter__()

    async def release() -> None:
        await asyncio.sleep(0.2)
        await holder.__aexit__(None, None, None)

    task = asyncio.create_task(release())
    started = time.monotonic()
    async with RedisLock(redis, "k", 30, wait_seconds=2, poll_seconds=0.05):
        waited = time.monotonic() - started
    await task
    assert 0.15 <= waited < 1.0


async def test_lock_wait_gives_up() -> None:
    redis = FakeRedis(decode_responses=True)
    async with RedisLock(redis, "k", 30):
        started = time.monotonic()
        with pytest.raises(LockBusyError):
            async with RedisLock(redis, "k", 30, wait_seconds=0.3, poll_seconds=0.05):
                pass
        assert 0.3 <= time.monotonic() - started < 1.0


async def test_lock_without_wait_refuses_at_once() -> None:
    redis = FakeRedis(decode_responses=True)
    async with RedisLock(redis, "k", 30):
        started = time.monotonic()
        with pytest.raises(LockBusyError):
            async with RedisLock(redis, "k", 30):
                pass
        assert time.monotonic() - started < 0.1


def test_default_wait_is_8_seconds() -> None:
    s = Settings(trading_execution_enabled=True)  # type: ignore[call-arg]
    assert s.exec_open_close_lock_wait_seconds == 8.0


# --- «Да, закрыть» ------------------------------------------------------------------------


def _lock(ctx) -> RedisLock:  # type: ignore[no-untyped-def]
    return RedisLock(ctx.redis, position_lock_key(ctx.uid, "XRP-USDT", "LONG"), 30)


@needs_db
async def test_close_waits_for_busy_lock(ctx) -> None:  # type: ignore[no-untyped-def]
    """Лок занят (повтор защиты) и освобождается через 0.4 с — кнопка дожидается
    и закрывает; пока ждёт — ключ close_wanted с TTL ожидание + 4 с; после — снят."""
    opening, _ = await _open(ctx, ALWAYS)
    holder = _lock(ctx)
    await holder.__aenter__()
    service = ctx.service(exec_open_fault=ALWAYS)
    task = asyncio.create_task(service.close_alarm(opening.id))
    await asyncio.sleep(0.15)
    assert 11 <= await ctx.redis.ttl(close_wanted_key(opening.id)) <= 12
    await asyncio.sleep(0.25)
    await holder.__aexit__(None, None, None)
    result = await task
    assert result.status is OpeningStatus.EMERGENCY_CLOSED and result.final, result.text
    assert not ctx.exchange.positions
    assert not await ctx.redis.exists(close_wanted_key(opening.id))


@needs_db
async def test_close_gives_up_after_wait(ctx) -> None:  # type: ignore[no-untyped-def]
    opening, _ = await _open(ctx, ALWAYS)
    markets = len(_posts(ctx, "post_market"))
    async with _lock(ctx):
        result = await ctx.service(
            exec_open_fault=ALWAYS, exec_open_close_lock_wait_seconds=0.3
        ).close_alarm(opening.id)
    assert not result.final and "Уже идёт действие" in result.text
    assert len(_posts(ctx, "post_market")) == markets and ctx.exchange.positions
    assert not await ctx.redis.exists(close_wanted_key(opening.id))   # снят в finally


@needs_db
async def test_repeat_yields_to_close_button(ctx, caplog) -> None:  # type: ignore[no-untyped-def]
    """Ключ close_wanted стоит — повтор защиты (и цикл) не берёт лок и не идёт
    на биржу."""
    opening, _ = await _open(ctx, ALWAYS)
    await ctx.redis.set(close_wanted_key(opening.id), "1", ex=12)
    ctx.exchange.calls.clear()
    settings = ctx.settings.model_copy(update={"exec_open_fault": ALWAYS})

    async def notify(*a, **kw):  # type: ignore[no-untyped-def]
        return None

    with caplog.at_level(logging.INFO):
        moved = await recover_one(ctx.db, settings, ctx.redis, ctx.factory, notify, opening.id)
    assert moved is False and ctx.exchange.calls == []
    assert any("уступил кнопке" in r.getMessage() for r in caplog.records)


@needs_db
async def test_close_during_running_repeat_closes_once(ctx) -> None:  # type: ignore[no-untyped-def]
    """Повтор защиты держит лок (биржа медленная) — «Да, закрыть» нажато в это
    время: дожидается, закрывает ровно одним ордером; повтор позицию не трогает."""
    opening, _ = await _open(ctx, ALWAYS)
    settings = ctx.settings.model_copy(update={"exec_open_fault": ALWAYS})
    original = ctx.exchange.get_open_orders

    async def slow_open_orders(*a, **kw):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.2)
        return await original(*a, **kw)

    ctx.exchange.get_open_orders = slow_open_orders

    async def notify(*a, **kw):  # type: ignore[no-untyped-def]
        return None

    repeat = asyncio.create_task(
        recover_one(ctx.db, settings, ctx.redis, ctx.factory, notify, opening.id)
    )
    lock_key = position_lock_key(ctx.uid, "XRP-USDT", "LONG")
    for _ in range(100):                       # дождаться, что повтор взял лок
        if await ctx.redis.exists(lock_key):
            break
        await asyncio.sleep(0.02)
    assert await ctx.redis.exists(lock_key)
    result = await ctx.service(exec_open_fault=ALWAYS).close_alarm(opening.id)
    await repeat
    assert result.status is OpeningStatus.EMERGENCY_CLOSED, result.text
    closes = [r for r in await _rows(ctx, opening.id)
              if r.role is OrderRole.CLOSE and r.status is OrderStatus.FILLED]
    assert len(closes) == 1 and not ctx.exchange.positions
