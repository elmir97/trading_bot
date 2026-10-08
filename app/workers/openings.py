"""Цикл открытий сделок из бота (05.10.2026): восстановление незавершённых
открытий каждые 15 с и один раз при старте бота до поллинга
(app/execution/opening/recovery.py). Сообщение в чат — с кнопкой «Позиция»,
если сделка записана."""

from __future__ import annotations

import time
from typing import Any

from aiogram import Bot

from app.bot.handlers.positions import position_keyboard
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.security import SecretCipher
from app.database.session import Database
from app.execution.opening.recovery import expire_stale_cards, recover_openings
from app.services.exchange_factory import ExchangeFactory
from app.trading.enums import TradeSide
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
        self, telegram_id: int, text: str, position: tuple[str, TradeSide] | None
    ) -> None:
        markup = position_keyboard(*position) if position is not None else None
        await send_notification(self._bot, telegram_id, text, reply_markup=markup)

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
