"""Перехват необработанных исключений в хендлерах.

Пользователь получает понятное сообщение, полный traceback уходит в лог.
Показывать stack trace в Telegram нельзя: он раскрывает внутренности
приложения и бесполезен для пользователя.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.dispatcher.event.bases import CancelHandler, SkipHandler
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.core.logging import get_logger

logger = get_logger(__name__)

_USER_MESSAGE = "Произошла внутренняя ошибка. Она записана в лог, попробуй ещё раз."


class ErrorMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except (SkipHandler, CancelHandler):
            # Управляющие исключения aiogram, а не ошибки: SkipHandler
            # передаёт событие следующему хендлеру. Если их проглотить,
            # пользователь получит «внутреннюю ошибку» на обычную команду.
            raise
        except TelegramAPIError:
            # Ошибки самого Telegram (лимиты, удалённое сообщение) —
            # отвечать пользователю через тот же канал бессмысленно.
            logger.exception("Ошибка Telegram API")
            raise
        except Exception:
            user_id = getattr(data.get("event_from_user"), "id", None)
            logger.exception("Необработанная ошибка", extra={"telegram_id": user_id})
            await self._notify(event)
            return None

    @staticmethod
    async def _notify(event: TelegramObject) -> None:
        try:
            if isinstance(event, Message):
                await event.answer(_USER_MESSAGE)
            elif isinstance(event, CallbackQuery):
                await event.answer(_USER_MESSAGE, show_alert=True)
        except TelegramAPIError:
            logger.exception("Не удалось доставить сообщение об ошибке")
