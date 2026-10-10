"""Цикл открытий сделок из бота (05.10.2026): восстановление незавершённых
открытий каждые 15 с и один раз при старте бота до поллинга
(app/execution/opening/recovery.py). Сообщение в чат — с кнопкой «Позиция»,
если сделка записана."""

from __future__ import annotations

import time
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select

from app.bot import outbox
from app.bot.handlers.open_trade import opening_keyboard
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.security import SecretCipher
from app.database.models.outgoing_message import OutgoingMessage, OutgoingMeta
from app.database.models.trade_opening import TradeOpening
from app.database.session import Database
from app.execution.opening.flow import working_exit_text
from app.execution.opening.recovery import expire_stale_cards, recover_openings
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
    ) -> None:
        self._bot = bot
        self._db = db
        self._settings = settings
        self._redis = redis
        self._factory = factory or ExchangeFactory(settings, cipher)
        self._fault_warned_at: float | None = None

    async def notify(
        self, telegram_id: int, text: str, position: tuple[str, TradeSide] | None, *,
        meta: OutgoingMeta | None = None, status: OpeningStatus | None = None,
        retire_working: int | None = None,
    ) -> None:
        if retire_working is not None:
            try:
                await self._retire_working(retire_working, status)
            except Exception:
                # Короткий итог на ⏳ вторичен: полный итог уходит всё равно.
                logger.exception(
                    "⏳ лимита не исправлен", extra={"opening_id": retire_working}
                )
        markup = opening_keyboard(
            status, meta.trade_opening_id if meta is not None else None, position
        )
        if meta is None:
            await send_notification(self._bot, telegram_id, text, reply_markup=markup)
            return
        await outbox.send(self._bot, self._db, meta, telegram_id, text, markup)

    async def _retire_working(self, opening_id: int, status: OpeningStatus | None) -> None:
        """Лимит вышел из WORKING (исполнен, истёк, снят на бирже): ⏳ —
        карточка «Да» и сообщения OPEN_WORKING журнала исходящих — правится в
        короткий итог, кнопка «Отменить лимит» снимается (Л3, A.1)."""
        async with self._db.session() as session:
            opening = await session.get(TradeOpening, opening_id)
            if opening is None:
                return
            rows = (await session.execute(
                select(OutgoingMessage.chat_id, OutgoingMessage.message_id).where(
                    OutgoingMessage.trade_opening_id == opening_id,
                    OutgoingMessage.kind == f"OPEN_{OpeningStatus.WORKING.value}",
                )
            )).all()
        targets = {(int(c), int(m)) for c, m in rows}
        if opening.chat_id is not None and opening.card_message_id is not None:
            targets.add((opening.chat_id, opening.card_message_id))
        final = status or opening.status
        text = working_exit_text(opening, final)
        meta = OutgoingMeta(
            user_id=opening.user_id, kind=f"OPEN_WORKING_{final.value}",
            trade_opening_id=opening.id, trade_id=opening.trade_id,
        )
        for chat_id, message_id in sorted(targets):
            try:
                await self._bot.edit_message_text(
                    text, chat_id=chat_id, message_id=message_id, reply_markup=None
                )
            except TelegramBadRequest as exc:
                # Удалено, старше 48 ч, уже исправлено — не роняет цикл.
                logger.warning(
                    "⏳ лимита не исправлен", extra={
                        "opening_id": opening_id, "message_id": message_id,
                        "error": str(exc)[:120],
                    },
                )
                continue
            await outbox.record(self._db, meta, chat_id=chat_id, message_id=message_id,
                                text=text)

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
            self._db, self._settings, self._redis, self._factory, self.notify
        )
