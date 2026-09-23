"""Тесты app/execution/journal_entry.py (шаг 15.5.4) — против настоящей БД.

ReadbackResult собирается напрямую: read-back проверен в test_readback.py,
здесь — только то, какую сделку журнал пишет из его итога. Ответ get_order
разбирается настоящим BingXClient._parse_order_fill (СИНТЕТИКА ДО 15.5.5).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.core.config import Settings
from app.database.models.execution_order import ExecutionOrder
from app.database.models.signal import SignalRecord
from app.database.models.signal_notification import SignalNotification
from app.database.models.trade import Trade
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.bingx import BingXClient
from app.execution.journal_entry import (
    NOTE_FILL_UNCONFIRMED,
    NOTE_STOP_UNCONFIRMED,
    NOTE_TAKE_UNCONFIRMED,
    record_entry_trade,
)
from app.execution.models import OrderRequest
from app.execution.readback import ConditionalOutcome, ConditionalState, ReadbackResult
from app.execution.service import build_entry_order_pending
from app.services.user_service import UserService
from app.trading.enums import (
    OrderSide,
    OrderStatus,
    SignalDirection,
    SignalLevel,
    TradeSide,
    TradeSource,
    TradeStatus,
)
from tests.conftest import cleanup_user

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

D = Decimal
NOW = datetime.now(UTC)
FILLED_AT = datetime(2026, 9, 24, 10, 0, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    async with db.session() as session:
        user = await UserService(
            UserRepository(session), StrategyRepository(session),
            MistakeTypeRepository(session), settings,
        ).get_or_create(telegram_id=unique_telegram_id())
        slot = SignalRecord(
            user_id=user.id, symbol="BTC-USDT", timeframe="1h", level=SignalLevel.READY,
            setup="Пробой с ретестом", direction=SignalDirection.LONG, fingerprint="fp",
            entry_low=D("100"), entry_high=D("101"), stop_loss=D("97"), take_profit=D("110"),
            detail="d", expires_at=NOW + timedelta(hours=4),
        )
        session.add(slot)
        await session.flush()
        notification = SignalNotification.snapshot_of(
            slot, notified_at=NOW, expires_at=NOW + timedelta(hours=4)
        )
        session.add(notification)
        await session.flush()
        yield session, user, slot, notification
        await cleanup_user(session, user)
    await db.dispose()


def _order(user_id: int, slot_id: int, nid: int) -> OrderRequest:
    return OrderRequest(
        user_id=user_id, signal_id=slot_id, notification_id=nid, symbol="BTC-USDT",
        side=OrderSide.BUY, position_side=TradeSide.LONG, quantity=D("0.010"),
        entry_price=D("100.1"), leverage=10, stop_loss=D("97.0"), take_profit=D("110.0"),
        notional=D("1"), margin=D("0.1"), risk_amount=D("0.031"),
        risk_percent=D("1.00"), risk_reward=D("3.19"),
    )


async def _entry(session, order: OrderRequest, status: OrderStatus) -> ExecutionOrder:  # type: ignore[no-untyped-def]
    row = build_entry_order_pending(order)
    row.status = status
    session.add(row)
    await session.flush()
    return row


def _fill(**overrides: object):  # type: ignore[no-untyped-def]
    raw: dict[str, object] = {
        "orderId": "9001", "status": "FILLED", "origQty": "0.010",
        "executedQty": "0.010", "avgPrice": "100.3", "commission": "-0.0005",
        "updateTime": int(FILLED_AT.timestamp() * 1000),
    }
    raw.update(overrides)
    return BingXClient._parse_order_fill(raw)


def _found(level: str, order_id: str) -> ConditionalState:
    return ConditionalState(ConditionalOutcome.FOUND, order_id, D(level))


def _readback(status: OrderStatus, **kw: object) -> ReadbackResult:
    fields: dict[str, object] = {
        "entry_status": status,
        "fill": _fill(),
        "stop": _found("97.0", "501"),
        "take": _found("110.0", "502"),
    }
    fields.update(kw)
    return ReadbackResult(**fields)  # type: ignore[arg-type]


async def _record(session, entry, order, notification, readback):  # type: ignore[no-untyped-def]
    return await record_entry_trade(
        session=session, entry_row=entry, order=order, notification=notification,
        timeframe="1h", readback=readback, account_balance=D("1000"), now=NOW,
    )


async def _trades(session, user_id: int) -> list[Trade]:  # type: ignore[no-untyped-def]
    return list(await session.scalars(select(Trade).where(Trade.user_id == user_id)))


async def test_filled_writes_trade_from_fact(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user, slot, n = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order, OrderStatus.FILLED)

    outcome = await _record(session, entry, order, n, _readback(OrderStatus.FILLED))

    assert outcome is not None and outcome.provisional is False and outcome.alarm is None
    t = outcome.trade
    assert (t.entry_price, t.quantity, t.fees) == (D("100.3"), D("0.010"), D("0.0005"))
    assert (t.notification_id, t.signal_id) == (n.id, slot.id)
    assert t.source is TradeSource.SIGNAL_EXECUTION
    assert t.status is TradeStatus.OPEN
    assert t.fill_confirmed is True
    assert (t.stop_loss, t.take_profit, t.leverage) == (D("97.0"), D("110.0"), 10)
    assert (t.risk_percent, t.risk_reward) == (D("1.00"), D("3.19"))
    assert t.account_balance_at_entry == D("1000")
    assert t.timeframe == "1h"
    assert t.opened_at == FILLED_AT
    assert t.strategy_id is not None and t.is_annotated is True  # «Пробой с ретестом»
    assert t.notes is None
    assert entry.trade_id == t.id


async def test_readback_incomplete_writes_provisional_trade(ctx) -> None:  # type: ignore[no-untyped-def]
    """Нет avgPrice (ReadbackIncomplete → fill None, вход SUBMITTED) —
    сделка всё равно пишется: плановая цена и объём, признак, пометка."""
    session, user, slot, n = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order, OrderStatus.SUBMITTED)

    outcome = await _record(
        session, entry, order, n, _readback(OrderStatus.SUBMITTED, fill=None)
    )

    assert outcome is not None and outcome.provisional is True
    t = outcome.trade
    assert (t.entry_price, t.quantity, t.fees) == (D("100.1"), D("0.010"), D("0"))
    assert t.fill_confirmed is False
    assert NOTE_FILL_UNCONFIRMED in (t.notes or "")
    assert t.opened_at == NOW
    assert outcome.entry_not_found is False


async def test_unknown_writes_provisional_trade(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user, slot, n = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order, OrderStatus.UNKNOWN)

    outcome = await _record(
        session, entry, order, n,
        ReadbackResult(entry_status=OrderStatus.UNKNOWN, warnings=["не подтвердила"]),
    )

    assert outcome is not None and outcome.provisional is True
    assert outcome.entry_not_found is True
    assert outcome.trade.fill_confirmed is False
    assert outcome.trade.stop_loss == D("97.0")
    assert NOTE_STOP_UNCONFIRMED in (outcome.trade.notes or "")


@pytest.mark.parametrize("status", [OrderStatus.REJECTED, OrderStatus.DRY_RUN, OrderStatus.REFUSED])
async def test_no_trade_for_refusal_or_dry_run(ctx, status) -> None:  # type: ignore[no-untyped-def]
    session, user, slot, n = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order, status)

    outcome = await _record(session, entry, order, n, ReadbackResult(entry_status=status))

    assert outcome is None
    assert await _trades(session, user.id) == []


async def test_repeat_does_not_create_second_trade(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user, slot, n = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order, OrderStatus.FILLED)
    first = await _record(session, entry, order, n, _readback(OrderStatus.FILLED))

    again = await _record(session, entry, order, n, _readback(OrderStatus.FILLED))

    assert again is not None and again.trade.id == first.trade.id
    assert len(await _trades(session, user.id)) == 1


async def test_parallel_writer_race_resolved_by_unique_index(ctx) -> None:  # type: ignore[no-untyped-def]
    """Сделка по уведомлению уже есть, а trade_id у ENTRY ещё не записан
    (параллельная запись reconciler 15.6) — вставка дубля упирается в
    uq_trades_notification_id, берётся существующая."""
    session, user, slot, n = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order, OrderStatus.FILLED)
    competitor = Trade(
        user_id=user.id, symbol="BTC-USDT", side=TradeSide.LONG, quantity=D("0.010"),
        entry_price=D("100.3"), opened_at=NOW, source=TradeSource.SIGNAL_EXECUTION,
        notification_id=n.id,
    )
    session.add(competitor)
    await session.flush()
    assert entry.trade_id is None

    outcome = await _record(session, entry, order, n, _readback(OrderStatus.FILLED))

    assert outcome is not None and outcome.trade.id == competitor.id
    assert entry.trade_id == competitor.id
    count = await session.scalar(
        select(func.count()).select_from(Trade).where(Trade.notification_id == n.id)
    )
    assert count == 1


@pytest.mark.parametrize(
    "state",
    [
        ConditionalState(ConditionalOutcome.RESCUE_FAILED, None, D("97.0")),
        ConditionalState(ConditionalOutcome.UNVERIFIED, None, D("97.0")),
        ConditionalState(ConditionalOutcome.AMBIGUOUS, None, D("97.0")),
    ],
    ids=["rescue_failed", "unverified", "ambiguous"],
)
async def test_unconfirmed_stop_keeps_planned_level_with_note(ctx, state) -> None:  # type: ignore[no-untyped-def]
    session, user, slot, n = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order, OrderStatus.FILLED)

    outcome = await _record(session, entry, order, n, _readback(OrderStatus.FILLED, stop=state))

    assert outcome is not None
    assert outcome.trade.stop_loss == D("97.0")
    assert NOTE_STOP_UNCONFIRMED in (outcome.trade.notes or "")
    assert NOTE_TAKE_UNCONFIRMED not in (outcome.trade.notes or "")


async def test_rescued_stop_level_is_used(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user, slot, n = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order, OrderStatus.FILLED)
    rescued = ConditionalState(ConditionalOutcome.RESCUED, "7001", D("97.0"))

    outcome = await _record(session, entry, order, n, _readback(OrderStatus.FILLED, stop=rescued))

    assert outcome is not None and outcome.trade.notes is None


async def test_entry_past_stop_alarm_and_trade_without_stop(ctx, caplog) -> None:  # type: ignore[no-untyped-def]
    """Проскальзывание дальше стопа: сделка пишется без стопа, пометка,
    logger.error и тревога пользователю."""
    session, user, slot, n = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order, OrderStatus.FILLED)

    with caplog.at_level("ERROR", logger="app.execution.journal_entry"):
        outcome = await _record(
            session, entry, order, n, _readback(OrderStatus.FILLED, fill=_fill(avgPrice="96.5"))
        )

    assert outcome is not None
    assert outcome.trade.stop_loss is None
    assert "за уровнем стопа" in (outcome.trade.notes or "")
    assert outcome.alarm == (
        "⚠️ Вход исполнен за уровнем стопа: BTC-USDT LONG, цена 96.5, стоп 97. "
        "Убыток больше заявленного — проверь позицию в BingX"
    )
    assert "Вход исполнен за уровнем стопа" in caplog.text
