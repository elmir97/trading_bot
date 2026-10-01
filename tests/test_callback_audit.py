"""app/execution/callback_audit.py и mask_telegram_id (префлайт 15.7).

Запись нажатия — против настоящей БД: своя сессия, коммит независимо от
транзакции апдейта, каскад от пользователя, lock_timeout вместо зависания.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.config import Settings
from app.core.security import mask_telegram_id
from app.database.models.execution_callback import ExecutionCallback
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.execution.callback_audit import record_callback
from app.services.user_service import UserService
from app.trading.enums import ExecutionCallbackAction

_needs_db = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (8867032918, "88…918"),
        (123456789, "12…789"),
        ("1234567890", "12…890"),
        (123456, "12…456"),
        (12345, "*****"),
        (777, "***"),
        (None, "—"),
    ],
)
def test_mask_telegram_id(value: int | str | None, expected: str) -> None:
    assert mask_telegram_id(value) == expected


def test_mask_never_contains_full_id() -> None:
    assert "8867032918" not in mask_telegram_id(8867032918)


async def _rows(db: Database, user_id: int) -> list[ExecutionCallback]:
    async with db.session() as s:
        stmt = select(ExecutionCallback).where(ExecutionCallback.user_id == user_id)
        return list((await s.scalars(stmt)).all())


async def _record(db: Database, user_id: int, notification_id: int | None = 42) -> None:
    await record_callback(
        db,
        user_id=user_id,
        telegram_id=1234567890,
        action=ExecutionCallbackAction.YES,
        notification_id=notification_id,
        raw_data=f"exn:yes:{notification_id}",
        chat_id=555,
        message_id=10,
        callback_query_id="cb10",
    )


@_needs_db
async def test_record_survives_rollback_of_update_transaction(unique_telegram_id) -> None:  # type: ignore[no-untyped-def]
    """Транзакция апдейта откатилась (хендлер упал) — нажатие осталось."""
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    try:
        async with db.session() as setup:
            users = UserRepository(setup)
            service = UserService(
                users, StrategyRepository(setup), MistakeTypeRepository(setup), settings
            )
            user = await service.get_or_create(telegram_id=unique_telegram_id())
        user_id = user.id

        with pytest.raises(RuntimeError):
            async with db.session():
                await _record(db, user_id)
                raise RuntimeError("хендлер упал")

        rows = await _rows(db, user_id)
        assert [(r.action, r.notification_id, r.chat_id, r.message_id) for r in rows] == [
            ("yes", 42, 555, 10)
        ]
        assert rows[0].callback_query_id == "cb10"

        await _record(db, user_id, notification_id=None)
        assert [r.notification_id for r in await _rows(db, user_id)] == [42, None]

        async with db.session() as s:
            await s.delete(await s.get(type(user), user_id))
        assert await _rows(db, user_id) == []
    finally:
        await db.dispose()


@_needs_db
async def test_uncommitted_user_fails_fast_not_hangs(unique_telegram_id) -> None:  # type: ignore[no-untyped-def]
    """Пользователь не закоммичен: своя сессия записи его не видит — сразу
    FK-ошибка (Postgres не ждёт чужую незакоммиченную вставку), не зависание.
    «Да» по такому нажатию откажет."""
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    try:
        async with db.session() as pending:
            service = UserService(
                UserRepository(pending), StrategyRepository(pending),
                MistakeTypeRepository(pending), settings,
            )
            user = await service.get_or_create(telegram_id=unique_telegram_id())
            with pytest.raises(IntegrityError, match="fk_execution_callbacks_user_id_users"):
                await asyncio.wait_for(_record(db, user.id), timeout=15)
            await pending.rollback()
    finally:
        await db.dispose()


@_needs_db
async def test_locked_user_row_fails_by_lock_timeout(unique_telegram_id) -> None:  # type: ignore[no-untyped-def]
    """Строка users под FOR UPDATE в чужой транзакции (удаление, смена ключа)
    конфликтует с FK-проверкой: lock_timeout даёт ошибку за секунды, а не
    ожидание до конца той транзакции."""
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    try:
        async with db.session() as setup:
            service = UserService(
                UserRepository(setup), StrategyRepository(setup),
                MistakeTypeRepository(setup), settings,
            )
            user = await service.get_or_create(telegram_id=unique_telegram_id())
        user_id = user.id

        async with db.session() as holder:
            await holder.execute(
                text("SELECT id FROM users WHERE id = :id FOR UPDATE"), {"id": user_id}
            )
            started = asyncio.get_running_loop().time()
            # Текст ошибки локализован сервером — сверяем класс asyncpg.
            with pytest.raises(DBAPIError, match="LockNotAvailableError"):
                await asyncio.wait_for(_record(db, user_id), timeout=15)
            assert asyncio.get_running_loop().time() - started < 10
            await holder.rollback()

        async with db.session() as s:
            await s.delete(await s.get(type(user), user_id))
    finally:
        await db.dispose()
