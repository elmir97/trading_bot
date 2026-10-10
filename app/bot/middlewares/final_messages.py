"""Кнопка под итоговым сообщением (A.1, 10.10.2026, баг №1).

Для нажатия кнопки один запрос в журнал исходящих: сообщение кнопки — итог?
Да — app/bot/messaging.edit_or_replace() и шаги мастера покажут экран новым
сообщением, итог в чате останется. Журнал не прочитан — считаем итогом:
лишнее новое сообщение безопаснее затёртого итога.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.bot import outbox
from app.bot.messaging import reset_final, set_final
from app.core.logging import get_logger
from app.database.session import Database

logger = get_logger(__name__)


class FinalMessageMiddleware(BaseMiddleware):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, CallbackQuery) or not isinstance(event.message, Message):
            return await handler(event, data)
        chat_id, message_id = event.message.chat.id, event.message.message_id
        try:
            final = message_id in await outbox.finals(self._db, chat_id, [message_id])
        except Exception:
            logger.warning(
                "Журнал исходящих не прочитан — сообщение кнопки считаю итогом",
                extra={"message_id": message_id}, exc_info=True,
            )
            final = True
        if not final:
            return await handler(event, data)
        token = set_final(chat_id, message_id)
        try:
            return await handler(event, data)
        finally:
            reset_final(token)
