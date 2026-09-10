"""Интеграционные тесты провижининга пользователя.

Требуют живой PostgreSQL: проверяем не мок, а фактические ограничения
целостности, дефолты и каскады.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.core.config import Settings
from app.database.models.mistake import SYSTEM_MISTAKES, MistakeType
from app.database.models.strategy import DEFAULT_STRATEGIES, Strategy
from app.database.repositories.strategy import (
    MistakeTypeRepository,
    StrategyRepository,
)
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.services.user_service import UserService

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="Нужен PostgreSQL: задай DATABASE_URL",
)


@pytest_asyncio.fixture
async def db() -> Database:  # type: ignore[misc]
    database = Database(Settings())  # type: ignore[call-arg]
    yield database
    await database.dispose()


@pytest_asyncio.fixture
async def service(db: Database):  # type: ignore[no-untyped-def]
    async with db.session() as session:
        yield UserService(
            UserRepository(session),
            StrategyRepository(session),
            MistakeTypeRepository(session),
            Settings(),  # type: ignore[call-arg]
        ), session


async def test_creates_user_with_defaults(service) -> None:  # type: ignore[no-untyped-def]
    svc, session = service
    user = await svc.get_or_create(telegram_id=555001, username="tester")

    assert user.id is not None

    plan = await UserRepository(session).get_trading_plan(user.id)
    assert plan is not None
    assert plan.risk_per_trade_percent == 2
    assert plan.max_daily_loss_percent == 6
    assert "BTC-USDT" in plan.allowed_symbols
    assert plan.allowed_timeframes == ["1h", "4h"]

    settings_row = await UserRepository(session).get_settings(user.id)
    assert settings_row is not None
    assert settings_row.timezone == "Asia/Yekaterinburg"
    assert settings_row.notifications["stop_loss_hit"] is True

    strategies = await StrategyRepository(session).list_active(user.id)
    assert len(strategies) == len(DEFAULT_STRATEGIES)
    assert any("ретест" in s.name.lower() for s in strategies)


async def test_get_or_create_is_idempotent(service) -> None:  # type: ignore[no-untyped-def]
    svc, session = service
    first = await svc.get_or_create(telegram_id=555002)
    second = await svc.get_or_create(telegram_id=555002)

    assert first.id == second.id

    count = await session.scalar(
        select(func.count()).select_from(Strategy).where(Strategy.user_id == first.id)
    )
    assert count == len(DEFAULT_STRATEGIES)


async def test_system_mistakes_seeded_once(service) -> None:  # type: ignore[no-untyped-def]
    svc, session = service
    await svc.ensure_system_mistakes()
    await svc.ensure_system_mistakes()

    count = await session.scalar(
        select(func.count()).select_from(MistakeType).where(
            MistakeType.is_system.is_(True)
        )
    )
    assert count == len(SYSTEM_MISTAKES)


async def test_users_are_isolated(service) -> None:  # type: ignore[no-untyped-def]
    """Стратегии одного пользователя не видны другому."""
    svc, session = service
    alice = await svc.get_or_create(telegram_id=555010)
    bob = await svc.get_or_create(telegram_id=555011)

    alice_strategies = await StrategyRepository(session).list_active(alice.id)
    bob_strategies = await StrategyRepository(session).list_active(bob.id)

    alice_ids = {s.id for s in alice_strategies}
    bob_ids = {s.id for s in bob_strategies}
    assert alice_ids and bob_ids
    assert alice_ids.isdisjoint(bob_ids)


async def test_mistake_catalog_can_be_restored(service) -> None:  # type: ignore[no-untyped-def]
    """Регрессия: справочник ошибок должен восстанавливаться.

    Он наполняется при старте приложения. Если справочник опустеет,
    пользователь молча перестанет получать вопрос про ошибки при
    закрытии сделки — шаг просто пропустится, и раздел «Анализ ошибок»
    останется пустым навсегда, без единого сообщения об ошибке.

    Поэтому ensure_system_mistakes обязан быть вызываемым повторно и
    восстанавливать справочник, а не только наполнять его один раз.
    """
    from sqlalchemy import delete

    svc, session = service
    await session.execute(delete(MistakeType).where(MistakeType.is_system.is_(True)))
    await session.flush()

    assert await MistakeTypeRepository(session).list_available(user_id=1) == []

    created = await svc.ensure_system_mistakes()
    assert created == len(SYSTEM_MISTAKES)

    available = await MistakeTypeRepository(session).list_available(user_id=1)
    assert len(available) == len(SYSTEM_MISTAKES)
