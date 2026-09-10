"""Сессия БД и текущий пользователь для каждого апдейта.

Одна транзакция на апдейт: хендлер либо отрабатывает целиком, либо все
его изменения откатываются. Это избавляет хендлеры от ручного управления
commit/rollback и исключает полузаписанные сделки.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User as TgUser

from app.core.config import Settings
from app.database.repositories.strategy import (
    MistakeTypeRepository,
    StrategyRepository,
)
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.services.user_service import UserService


class DatabaseMiddleware(BaseMiddleware):
    """Открывает сессию, разрешает пользователя, прокидывает зависимости."""

    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)

        async with self._db.session() as session:
            users = UserRepository(session)
            strategies = StrategyRepository(session)
            mistakes = MistakeTypeRepository(session)

            user_service = UserService(users, strategies, mistakes, self._settings)
            user = await user_service.get_or_create(
                telegram_id=tg_user.id,
                username=tg_user.username,
                first_name=tg_user.first_name,
            )

            data["session"] = session
            data["user"] = user
            data["user_service"] = user_service

            return await handler(event, data)
