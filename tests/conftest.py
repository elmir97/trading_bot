"""Общие фикстуры для интеграционных тестов."""

from __future__ import annotations

import itertools
from collections.abc import Callable

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.user import User

# Общий на весь прогон счётчик, а не по одному на файл: гарантирует
# отсутствие коллизий telegram_id не только внутри файла, но и между
# файлами. Замена timestamp() % 90_000 — та схема циклилась каждые 90 мс
# (период модуля, не зависит от точности множителя) и закономерно, не
# случайно, совпадала у соседних по времени тестов. Начало отсчёта далеко
# от telegram_id=424242 — он зарезервирован под scripts/smoke_check.py.
_telegram_id_seq = itertools.count(500_000)


@pytest.fixture
def unique_telegram_id() -> Callable[[], int]:
    """Фабрика, а не готовое число: тестам, которым в одном прогоне нужно
    несколько разных пользователей (см. test_execution_orders.py), одно
    и то же кэшированное значение фикстуры не подошло бы — каждый вызов
    обязан отдавать новый telegram_id."""
    return lambda: next(_telegram_id_seq)


async def cleanup_user(session: AsyncSession, user: User) -> None:
    """Убирает тестового пользователя за собой — каскады FK (ondelete=
    "CASCADE") заберут credentials/сигналы/сделки на его user_id.

    Проверка persistent нужна тестам, которые сами делают session.rollback()
    после ожидаемого IntegrityError (например test_settings.py — UNIQUE-
    констрейнты): откат уже отменил и вставку пользователя в этой же
    незакоммиченной транзакции, session.delete() на уже не persistent
    объекте упал бы InvalidRequestError "Instance is not persisted".
    """
    if inspect(user).persistent:
        await session.delete(user)
