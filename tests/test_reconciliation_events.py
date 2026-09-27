"""reconciliation_events (шаг 15.6): дедуп уведомлений reconciler.

Одно открытое расхождение — одна строка (частичный UNIQUE по user_id,
dedup_key без resolved_at); после разрешения то же расхождение может
появиться снова новой строкой."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.database.models.reconciliation_event import ReconciliationEvent
from app.database.repositories.reconciliation_event import ReconciliationEventRepository
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.services.user_service import UserService
from app.trading.enums import ReconciliationKind
from tests.conftest import cleanup_user

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")


@pytest_asyncio.fixture
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    async with db.session() as session:
        svc = UserService(
            UserRepository(session), StrategyRepository(session),
            MistakeTypeRepository(session), settings,
        )
        user = await svc.get_or_create(telegram_id=unique_telegram_id())
        yield session, user
        await cleanup_user(session, user)
    await db.dispose()


def _event(user_id: int, key: str = "stop_missing:trade:1") -> ReconciliationEvent:
    return ReconciliationEvent(
        user_id=user_id, symbol="LINK-USDT", kind=ReconciliationKind.STOP_MISSING,
        dedup_key=key, detail="стопа нет в openOrders",
    )


async def test_second_open_event_with_same_key_is_rejected(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user = ctx
    repo = ReconciliationEventRepository(session)
    repo.add(_event(user.id))
    await repo.flush()
    assert (await repo.get_open(user.id, "stop_missing:trade:1")) is not None

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            repo.add(_event(user.id))
            await repo.flush()


async def test_same_key_allowed_again_after_resolution(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user = ctx
    repo = ReconciliationEventRepository(session)
    first = repo.add(_event(user.id))
    await repo.flush()
    first.resolved_at = datetime.now(UTC)
    await repo.flush()
    assert (await repo.get_open(user.id, "stop_missing:trade:1")) is None

    repo.add(_event(user.id))
    await repo.flush()
    assert (await repo.get_open(user.id, "stop_missing:trade:1")) is not None
