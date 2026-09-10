"""Базовый репозиторий.

Главное правило слоя: любой метод, работающий с пользовательскими данными,
принимает user_id обязательным аргументом. Изоляция обеспечивается здесь,
а не в сервисах — забыть фильтр в одном сервисе легко, а последствием
будет показ чужих сделок.
"""

from __future__ import annotations

from typing import Generic, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.base import Base

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, entity_id: int, user_id: int) -> ModelT | None:
        """Получение по id всегда в связке с user_id.

        Метод без user_id не предусмотрен намеренно: он стал бы удобным
        способом случайно достать чужую запись по угаданному id.
        """
        from sqlalchemy import select

        stmt = (
            select(self.model)
            .where(self.model.id == entity_id)  # type: ignore[attr-defined]
            .where(self.model.user_id == user_id)  # type: ignore[attr-defined]
        )
        return await self.session.scalar(stmt)

    def add(self, entity: ModelT) -> ModelT:
        self.session.add(entity)
        return entity

    async def delete(self, entity: ModelT) -> None:
        await self.session.delete(entity)

    async def flush(self) -> None:
        """Проставляет id новым объектам, не завершая транзакцию."""
        await self.session.flush()
