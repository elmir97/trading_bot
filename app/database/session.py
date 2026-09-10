"""Подключение к PostgreSQL: движок, фабрика сессий, базовый класс моделей."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from app.core.config import Settings
from app.database.base import Base

__all__ = ["Base", "Database"]


class Database:
    """Владеет движком и фабрикой сессий.

    Не глобальная переменная, а объект: так его можно подменить в тестах
    и корректно закрыть при остановке приложения.
    """

    def __init__(self, settings: Settings) -> None:
        self._engine: AsyncEngine = create_async_engine(
            settings.database_url.get_secret_value(),
            echo=settings.db_echo,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,  # отсекает соединения, убитые простоем
        )
        self._session_factory = async_sessionmaker(
            bind=self._engine,
            expire_on_commit=False,  # объекты остаются пригодны после commit
            autoflush=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Сессия с транзакцией: commit при успехе, rollback при исключении."""
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def healthcheck(self) -> bool:
        from sqlalchemy import text

        async with self._engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True

    async def dispose(self) -> None:
        await self._engine.dispose()
