"""B.1 (10.10.2026): быстрые повторы защиты после ALARM — 1.5 / 3 / 5 с от
тревоги, не ждать цикла 15 с (Т3: без стопа 18.7 с; цель владельца ≤ 8 с).
Тот же проход, что у цикла, под тем же локом; тревога снята — повторы
прекращаются; исчерпаны — дальше цикл. Оба вида тревоги («стоп не встал и
закрыть не удалось», «стоп не подтверждён»). Фейковая биржа и БД —
test_opening_confirm.opening_context, паузы — подменённый sleep."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiogram.types import CallbackQuery, Chat, Message
from pydantic import ValidationError

from app.bot.handlers import open_trade
from app.core.config import Settings
from app.core.locks import RedisLock, position_lock_key
from app.execution.opening.service import ConfirmOutcome
from app.trading.enums import OpeningStatus, TradeSide
from app.workers.openings import OpeningsWorker
from tests.test_opening_confirm import opening_context
from tests.test_opening_faults import BASE, _open, _posts, _recover, _stops_on_exchange

needs_db = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")
T3 = "skip_attached_stop,fail_backup_stop,fail_emergency_close"
T5 = "skip_attached_stop,hide_backup_stop,fail_emergency_close"
ALWAYS = "skip_attached_stop,fail_backup_stop_always"


@pytest_asyncio.fixture
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    async for c in opening_context(unique_telegram_id()):
        yield c


class _Bot:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.edited: list[int] = []
        self.next_id = 700

    async def send_message(self, chat_id, text, reply_markup=None, **_):  # type: ignore[no-untyped-def]
        self.next_id += 1
        self.sent.append(text)
        return Message(message_id=self.next_id, date=datetime.now(UTC),
                       chat=Chat(id=chat_id, type="private"), text=text)

    async def edit_message_text(self, text, *, chat_id, message_id, reply_markup=None):  # type: ignore[no-untyped-def]
        self.edited.append(message_id)


def _worker(ctx, fault: str, bot: _Bot, sleeps: list[float], hook=None):  # type: ignore[no-untyped-def]
    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if hook is not None:
            await hook(len(sleeps))

    settings = ctx.settings.model_copy(update={"exec_open_fault": fault})
    return OpeningsWorker(bot, ctx.db, settings, None, ctx.redis,  # type: ignore[arg-type]
                          factory=ctx.factory, sleep=sleep)


async def _run(worker: OpeningsWorker, opening_id: int) -> None:
    worker.retry_alarm(opening_id)
    task = worker._alarm_tasks[opening_id]
    await task


def test_retry_offsets_setting() -> None:
    assert Settings(**BASE).exec_open_alarm_retry_offsets == (1.5, 3.0, 5.0)  # type: ignore[arg-type]
    s = Settings(**BASE, exec_open_alarm_retry_seconds="1,2")  # type: ignore[arg-type]
    assert s.exec_open_alarm_retry_offsets == (1.0, 2.0)
    for bad in ("3,1.5", "0,1", "1,x", "1,1"):
        with pytest.raises(ValidationError, match="EXEC_OPEN_ALARM_RETRY_SECONDS"):
            Settings(**BASE, exec_open_alarm_retry_seconds=bad)  # type: ignore[arg-type]


@needs_db
async def test_first_retry_places_stop(ctx) -> None:  # type: ignore[no-untyped-def]
    """Т3: тревога → первый повтор через 1.5 с ставит s2 → DONE, повторы
    прекращены; ALARM в чате правится, итог — новым сообщением."""
    opening, out = await _open(ctx, T3)
    assert out.status is OpeningStatus.ALARM
    bot, sleeps = _Bot(), []
    await _run(_worker(ctx, T3, bot, sleeps), opening.id)
    assert sleeps == [1.5]
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.DONE
    [stop] = _stops_on_exchange(ctx)
    assert stop.client_order_id == f"to{opening.id}u{ctx.uid}s2"
    assert len(bot.sent) == 1 and "со 2-й попытки" in bot.sent[0]


@needs_db
async def test_unconfirmed_alarm_retried_too(ctx) -> None:  # type: ignore[no-untyped-def]
    """Т5 («стоп не подтверждён»): повтор находит s1 — DONE без s2."""
    opening, out = await _open(ctx, T5)
    assert out.status is OpeningStatus.ALARM
    await _run(_worker(ctx, T5, _Bot(), []), opening.id)
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.DONE
    assert len(_posts(ctx, "post_conditional")) == 1                # s2 не отправлялся


@needs_db
async def test_exhausted_retries_leave_alarm_to_cycle(ctx, caplog) -> None:  # type: ignore[no-untyped-def]
    opening, _ = await _open(ctx, ALWAYS)
    sleeps: list[float] = []
    with caplog.at_level(logging.INFO):
        await _run(_worker(ctx, ALWAYS, _Bot(), sleeps), opening.id)
    assert sleeps == [1.5, 1.5, 2.0]                                # от тревоги: 1.5, 3, 5
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.ALARM
    assert [r.attempt for r in caplog.records  # type: ignore[attr-defined]
            if r.getMessage() == "Быстрый повтор после тревоги"] == [1, 2, 3]
    assert any("исчерпаны" in r.getMessage() for r in caplog.records)
    assert _posts(ctx, "post_conditional") == []


@needs_db
async def test_busy_lock_skips_attempt(ctx) -> None:  # type: ignore[no-untyped-def]
    """Лок позиции занят (цикл или «Закрыть») — повтор пропущен, следующий —
    по расписанию; двух стопов нет."""
    opening, _ = await _open(ctx, T3)
    key = position_lock_key(ctx.uid, opening.symbol, opening.side.value)
    lock = RedisLock(ctx.redis, key, 60)
    await lock.__aenter__()

    async def hook(n: int) -> None:
        if n == 2:
            await lock.__aexit__(None, None, None)    # освободили перед 2-м повтором

    sleeps: list[float] = []
    await _run(_worker(ctx, T3, _Bot(), sleeps, hook), opening.id)
    assert sleeps == [1.5, 1.5]
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.DONE
    assert len(_stops_on_exchange(ctx)) == 1


@needs_db
async def test_one_task_per_opening_and_close_cancels(ctx) -> None:  # type: ignore[no-untyped-def]
    opening, _ = await _open(ctx, ALWAYS)
    gate = asyncio.Event()

    async def sleep(seconds: float) -> None:
        await gate.wait()

    settings = ctx.settings.model_copy(update={"exec_open_fault": ALWAYS})
    worker = OpeningsWorker(_Bot(), ctx.db, settings, None, ctx.redis,  # type: ignore[arg-type]
                            factory=ctx.factory, sleep=sleep)
    worker.retry_alarm(opening.id)
    first = worker._alarm_tasks[opening.id]
    worker.retry_alarm(opening.id)
    assert worker._alarm_tasks[opening.id] is first
    await worker.close()
    assert first.cancelled() and opening.id not in worker._alarm_tasks


@needs_db
async def test_cycle_hook_only_on_new_alarm(ctx) -> None:  # type: ignore[no-untyped-def]
    """Цикл зовёт on_alarm, только когда открытие перешло в ALARM, — не на
    каждом цикле стоящей тревоги."""
    from app.execution.opening.recovery import recover_openings

    opening, _ = await _open(ctx, ALWAYS)
    settings = ctx.settings.model_copy(update={"exec_open_fault": ALWAYS})
    seen: list[int] = []

    async def notify(*a: Any, **k: Any) -> None:
        pass

    await recover_openings(ctx.db, settings, ctx.redis, ctx.factory, notify,
                           on_alarm=seen.append)
    assert seen == []                                    # уже был ALARM
    opening.status = OpeningStatus.FILLED
    await ctx.session.commit()
    await recover_openings(ctx.db, settings, ctx.redis, ctx.factory, notify,
                           on_alarm=seen.append)
    assert seen == [opening.id]


async def test_confirm_with_alarm_starts_retries(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """«Открыть» вернул тревогу — повторы запускаются после записи итога."""
    order: list[str] = []
    service = MagicMock()
    service.confirm = AsyncMock(return_value=ConfirmOutcome("🚨🚨", OpeningStatus.ALARM))
    service.load = AsyncMock(return_value=SimpleNamespace(symbol="XRP-USDT",
                                                          side=TradeSide.LONG))
    monkeypatch.setattr(open_trade, "_service", lambda *a, **k: service)
    monkeypatch.setattr(open_trade, "_audit", AsyncMock(return_value=True))
    monkeypatch.setattr(open_trade, "_is_current", AsyncMock(return_value=False))

    async def edit(*a: Any, **k: Any) -> None:
        order.append("edit")

    monkeypatch.setattr(open_trade.outbox, "edit", edit)
    message = MagicMock(spec=Message)
    message.message_id = 600
    callback = MagicMock(spec=CallbackQuery)
    callback.data = f"{open_trade.OpenCB.YES}15"
    callback.message = message
    callback.answer = AsyncMock()
    await open_trade.confirm_open(
        callback, MagicMock(), SimpleNamespace(id=1), MagicMock(), MagicMock(), MagicMock(),
        MagicMock(), MagicMock(), alarm_retry=lambda i: order.append(f"retry:{i}"),
    )
    assert order == ["edit", "retry:15"]


@needs_db
async def test_without_fast_retry_cycle_still_works(ctx) -> None:  # type: ignore[no-untyped-def]
    """Повторы — ускорение, не замена: цикл по-прежнему ставит стоп."""
    opening, _ = await _open(ctx, T3)
    await _recover(ctx, T3)
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.DONE


async def test_cycle_passes_retry_hook(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Цикл и восстановление при старте (worker.run) запускают повторы."""
    from app.workers import openings

    seen: dict[str, Any] = {}

    async def recover(*a: Any, **k: Any) -> int:
        seen.update(k)
        return 0

    monkeypatch.setattr(openings, "recover_openings", recover)
    monkeypatch.setattr(openings, "expire_stale_cards", AsyncMock(return_value=0))
    worker = OpeningsWorker(None, None, Settings(**BASE), None, object(),  # type: ignore[arg-type]
                            factory=object())
    await worker.run()
    assert seen["on_alarm"] == worker.retry_alarm
