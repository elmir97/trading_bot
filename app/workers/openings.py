"""Цикл открытий сделок из бота (05.10.2026): восстановление незавершённых
открытий каждые 15 с и один раз при старте бота до поллинга
(app/execution/opening/recovery.py). Сообщение в чат — с кнопкой «Позиция»,
если сделка записана."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot
from sqlalchemy import select

from app.bot import opening_messages, outbox
from app.bot.handlers.open_trade import opening_keyboard
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.security import SecretCipher
from app.database.models.outgoing_message import OutgoingMeta
from app.database.models.trade_opening import TradeOpening
from app.database.session import Database
from app.execution.opening.recovery import (
    expire_stale_cards,
    recover_one,
    recover_openings,
)
from app.services.exchange_factory import ExchangeFactory
from app.trading.enums import OpeningStatus, TradeSide
from app.workers.notifier import send_notification

logger = get_logger(__name__)

FAULT_WARN_EVERY = 3600.0


class OpeningsWorker:
    def __init__(
        self,
        bot: Bot,
        db: Database,
        settings: Settings,
        cipher: SecretCipher,
        redis: Any,
        *,
        factory: Any = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._bot = bot
        self._db = db
        self._settings = settings
        self._redis = redis
        self._factory = factory or ExchangeFactory(settings, cipher)
        self._fault_warned_at: float | None = None
        self._sleep = sleep
        # B.1: задачи быстрых повторов после тревоги — по одной на открытие.
        self._alarm_tasks: dict[int, asyncio.Task[None]] = {}

    async def notify(
        self, telegram_id: int, text: str, position: tuple[str, TradeSide] | None, *,
        meta: OutgoingMeta | None = None, status: OpeningStatus | None = None,
        retire_working: int | None = None, resolve_alarm: int | None = None,
    ) -> None:
        # Прежние сообщения (⏳, ALARM) — до нового итога; вторичны: сбой не
        # мешает полному итогу.
        if retire_working is not None:
            try:
                await opening_messages.retire_working(
                    self._bot, self._db, retire_working, status
                )
            except Exception:
                logger.exception("⏳ лимита не исправлен", extra={"opening_id": retire_working})
        if resolve_alarm is not None and status is not None:
            try:
                await opening_messages.resolve_alarm(self._bot, self._db, resolve_alarm, status)
            except Exception:
                logger.exception(
                    "Сообщение тревоги не исправлено", extra={"opening_id": resolve_alarm}
                )
        markup = opening_keyboard(
            status, meta.trade_opening_id if meta is not None else None, position
        )
        if meta is None:
            await send_notification(self._bot, telegram_id, text, reply_markup=markup)
            return
        await outbox.send(self._bot, self._db, meta, telegram_id, text, markup)

    def retry_alarm(self, opening_id: int) -> None:
        """B.1 (10.10.2026): тревога объявлена — повторы защиты через
        exec_open_alarm_retry_seconds от неё (1.5 / 3 / 5 с), не дожидаясь
        цикла 15 с. Тот же проход, что у цикла (лок позиции: занят — повтор
        пропущен). Тревога снята — повторы прекращаются; исчерпаны — дальше
        цикл. Рестарт задачу теряет — её дело берёт восстановление при старте."""
        task = self._alarm_tasks.get(opening_id)
        if task is not None and not task.done():
            return
        self._alarm_tasks[opening_id] = asyncio.create_task(self._retry_alarm(opening_id))

    async def _retry_alarm(self, opening_id: int) -> None:
        elapsed = 0.0
        try:
            for attempt, offset in enumerate(self._settings.exec_open_alarm_retry_offsets, 1):
                await self._sleep(max(offset - elapsed, 0.0))
                elapsed = offset
                if self._redis is None:
                    return
                await recover_one(
                    self._db, self._settings, self._redis, self._factory, self.notify,
                    opening_id,
                )
                async with self._db.session() as session:
                    status = await session.scalar(
                        select(TradeOpening.status).where(TradeOpening.id == opening_id)
                    )
                logger.info(
                    "Быстрый повтор после тревоги", extra={
                        "opening_id": opening_id, "attempt": attempt, "offset_s": offset,
                        "status": status.value if status is not None else None,
                    },
                )
                if status is not OpeningStatus.ALARM:
                    return
            logger.warning(
                "Быстрые повторы исчерпаны — тревога остаётся, дальше цикл",
                extra={"opening_id": opening_id},
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Быстрый повтор после тревоги упал", extra={"opening_id": opening_id}
            )
        finally:
            self._alarm_tasks.pop(opening_id, None)

    async def close(self) -> None:
        """Остановка бота: незавершённые повторы снимаются (их дело возьмёт
        восстановление при старте)."""
        tasks = list(self._alarm_tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._alarm_tasks.clear()   # отменённая до первого шага не дойдёт до finally

    async def run(self) -> None:
        faults = self._settings.exec_open_faults
        if faults:
            now = time.monotonic()
            if self._fault_warned_at is None or now - self._fault_warned_at >= FAULT_WARN_EVERY:
                # Включённый на время проверки сбой нельзя забыть: раз в час в лог.
                logger.warning(
                    "EXEC_OPEN_FAULT включён: %s — только на время проверки, снять после",
                    ",".join(sorted(faults)), extra={"faults": sorted(faults)},
                )
                self._fault_warned_at = now
        await expire_stale_cards(self._db, self._settings)
        if self._redis is None:
            return
        await recover_openings(
            self._db, self._settings, self._redis, self._factory, self.notify,
            on_alarm=self.retry_alarm,
        )
