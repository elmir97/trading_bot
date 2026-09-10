"""Whitelist доступа.

Бот персональный, но токен публичен для любого, кто узнает @username.
Отсекаем чужих на входе, до того как запрос дойдёт до бизнес-логики.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from app.core.logging import get_logger

logger = get_logger(__name__)


class AccessMiddleware(BaseMiddleware):
    def __init__(self, allowed_ids: frozenset[int]) -> None:
        self._allowed_ids = allowed_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not self._allowed_ids:  # whitelist не настроен — пропускаем всех
            return await handler(event, data)

        user: User | None = data.get("event_from_user")
        if user is None or user.id not in self._allowed_ids:
            if user is not None:
                logger.warning("Отклонён доступ", extra={"telegram_id": user.id})
            await self._reject(event)
            return None

        return await handler(event, data)

    @staticmethod
    async def _reject(event: TelegramObject) -> None:
        text = "Доступ к этому боту ограничен."
        if isinstance(event, Message):
            await event.answer(text)
        elif isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
