"""Тесты таблицы signal_notifications и её репозитория (шаг 15.5.2а).

Против настоящей БД: проверяются снимок, выборки для сканера (latest_for_slot)
и гварда SETUP_ALREADY_TRADED (exists_traded), подсчёт для сводки и каскад
при удалении слота. Весь API файла новый — на коде до 15.5.2а каждый тест
падал бы ImportError.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.config import Settings
from app.database.models.signal import SignalRecord
from app.database.models.signal_notification import SignalNotification
from app.database.repositories.signal_notification import SignalNotificationRepository
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.services.user_service import UserService
from app.trading.enums import SignalDirection, SignalLevel
from tests.conftest import cleanup_user

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

D = Decimal
NOW = datetime.now(UTC)


def _slot(user_id: int, **overrides: object) -> SignalRecord:
    fields: dict[str, object] = {
        "user_id": user_id,
        "symbol": "BTC-USDT",
        "timeframe": "1h",
        "level": SignalLevel.READY,
        "setup": "Пробой с ретестом",
        "direction": SignalDirection.LONG,
        "fingerprint": "fp-a",
        "entry_low": D("100"),
        "entry_high": D("101"),
        "stop_loss": D("95"),
        "take_profit": D("115"),
        "confidence": 7,
        "detail": "detail",
        "expires_at": NOW + timedelta(hours=4),
    }
    fields.update(overrides)
    return SignalRecord(**fields)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    async with db.session() as session:
        user_service = UserService(
            UserRepository(session),
            StrategyRepository(session),
            MistakeTypeRepository(session),
            settings,
        )
        user = await user_service.get_or_create(telegram_id=unique_telegram_id())
        slot = _slot(user.id)
        session.add(slot)
        await session.flush()
        yield session, user, slot, SignalNotificationRepository(session)
        await cleanup_user(session, user)
    await db.dispose()


def _notify(slot: SignalRecord, at: datetime, **overrides: object) -> SignalNotification:
    notification = SignalNotification.snapshot_of(
        slot, notified_at=at, expires_at=at + timedelta(hours=4)
    )
    for key, value in overrides.items():
        setattr(notification, key, value)
    return notification


async def test_snapshot_copies_slot_and_survives_slot_rewrite(ctx) -> None:  # type: ignore[no-untyped-def]
    """Снимок — копия слота на момент уведомления: последующая перезапись
    слота сканером (новые уровни, направление, fingerprint) его не трогает."""
    session, user, slot, repo = ctx
    notification = repo.add(_notify(slot, NOW))
    await repo.flush()
    notification_id, slot_id, user_id = notification.id, slot.id, user.id

    slot.fingerprint = "fp-b"
    slot.direction = SignalDirection.SHORT
    slot.stop_loss = D("120")
    await session.flush()
    session.expire_all()

    fetched = await repo.get(notification_id, user_id)
    assert fetched is not None
    assert fetched.signal_id == slot_id
    assert fetched.fingerprint == "fp-a"
    assert fetched.direction is SignalDirection.LONG
    assert fetched.stop_loss == D("95")
    assert fetched.take_profit == D("115")
    assert fetched.entry_low == D("100") and fetched.entry_high == D("101")
    assert fetched.confidence == 7
    assert fetched.expires_at == NOW + timedelta(hours=4)
    assert fetched.trade_opened_at is None


async def test_get_foreign_user_returns_none(ctx) -> None:  # type: ignore[no-untyped-def]
    _session, user, slot, repo = ctx
    notification = repo.add(_notify(slot, NOW))
    await repo.flush()

    assert await repo.get(notification.id, user.id + 1_000_000) is None
    assert await repo.get(notification.id + 1_000_000, user.id) is None


async def test_latest_for_slot_returns_most_recent(ctx) -> None:  # type: ignore[no-untyped-def]
    _session, _user, slot, repo = ctx
    older = repo.add(_notify(slot, NOW - timedelta(hours=5)))
    newer = repo.add(_notify(slot, NOW, fingerprint="fp-b"))
    await repo.flush()

    latest = await repo.latest_for_slot(slot.id)
    assert latest is not None
    assert latest.id == newer.id
    assert latest.id != older.id


async def test_latest_for_slot_empty(ctx) -> None:  # type: ignore[no-untyped-def]
    _session, _user, slot, repo = ctx
    assert await repo.latest_for_slot(slot.id) is None


async def test_exists_traded_matches_slot_and_fingerprint_only(ctx) -> None:  # type: ignore[no-untyped-def]
    """SETUP_ALREADY_TRADED: только тот же слот + тот же fingerprint с
    непустым trade_opened_at. Другой fingerprint того же слота и
    неотработанное уведомление того же сетапа — не считаются."""
    session, user, slot, repo = ctx
    repo.add(_notify(slot, NOW - timedelta(hours=1)))  # тот же сетап, без сделки
    repo.add(_notify(slot, NOW, fingerprint="fp-b", trade_opened_at=NOW))
    await repo.flush()

    assert await repo.exists_traded(slot.id, "fp-a") is False
    assert await repo.exists_traded(slot.id, "fp-b") is True

    other_slot = _slot(user.id, symbol="ETH-USDT")
    session.add(other_slot)
    await session.flush()
    assert await repo.exists_traded(other_slot.id, "fp-b") is False


async def test_count_ready_between_counts_events_not_slots(ctx) -> None:  # type: ignore[no-untyped-def]
    """Сводка: два READY-уведомления одного слота в окне — два события.
    FORMING и уведомления вне окна не считаются."""
    session, user, slot, repo = ctx
    repo.add(_notify(slot, NOW - timedelta(hours=2)))
    repo.add(_notify(slot, NOW - timedelta(hours=1), fingerprint="fp-b"))
    repo.add(_notify(slot, NOW - timedelta(hours=30)))  # вне окна
    forming = _slot(user.id, level=SignalLevel.FORMING, direction=None)
    session.add(forming)
    await session.flush()
    repo.add(_notify(forming, NOW - timedelta(hours=1)))
    await repo.flush()

    count = await repo.count_ready_between(user.id, NOW - timedelta(hours=24), NOW)
    assert count == 2


async def test_deleting_slot_cascades_to_notifications(ctx) -> None:  # type: ignore[no-untyped-def]
    session, _user, slot, repo = ctx
    notification = repo.add(_notify(slot, NOW))
    await repo.flush()
    notification_id = notification.id

    await session.delete(slot)
    await session.flush()
    session.expire_all()

    rows = await session.scalars(
        select(SignalNotification).where(SignalNotification.id == notification_id)
    )
    assert list(rows) == []
