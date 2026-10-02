"""Схема M1 (миграция 5951467e6d9a, этапы 3–4) против настоящей БД:
position_actions с CHECK, execution_orders.position_action_id (SET NULL),
execution_callbacks — действия pm_* длиной до 16 и position_action_id,
trades.initial_stop_loss, статус CANCELLED (написание BingX)."""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.database.models.execution_callback import ExecutionCallback
from app.database.models.execution_order import ExecutionOrder
from app.database.models.position_action import PositionAction
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.trade import TradeRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.services.user_service import UserService
from app.trading.enums import (
    ExecutionCallbackAction,
    OrderRole,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionActionKind,
    PositionActionStatus,
    TradeSide,
)
from app.trading.journal import TradeJournal
from tests.conftest import cleanup_user

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

D = Decimal


@pytest_asyncio.fixture
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    async with db.session() as session:
        user = await UserService(
            UserRepository(session), StrategyRepository(session),
            MistakeTypeRepository(session), settings,
        ).get_or_create(telegram_id=unique_telegram_id())
        await session.commit()
        user_id = user.id
        yield session, user
        await session.rollback()
        fresh = await UserRepository(session).get_by_id(user_id)
        await cleanup_user(session, fresh)
        await session.commit()
    await db.dispose()


def _action(user_id: int, **overrides: object) -> PositionAction:
    fields: dict[str, object] = dict(
        user_id=user_id, symbol="XRP-USDT", side=TradeSide.LONG,
        position_id="2105907655281221634", kind=PositionActionKind.MOVE_STOP,
        params={"breakeven": True}, mark_price=D("1.5263"), position_qty=D(30),
        current_stop=D("1.4795"), new_level=D("1.5268"),
    )
    fields.update(overrides)
    return PositionAction(**fields)  # type: ignore[arg-type]


async def test_action_defaults_and_roundtrip(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user = ctx
    uid = user.id
    action = _action(uid)
    session.add(action)
    await session.commit()
    fetched = await session.scalar(select(PositionAction).where(PositionAction.id == action.id))
    assert fetched is not None
    assert fetched.status is PositionActionStatus.CARD and fetched.risk_increase is False
    assert fetched.params == {"breakeven": True}
    assert fetched.position_id == "2105907655281221634"
    assert fetched.new_level == D("1.5268")


@pytest.mark.parametrize(("column", "value"), [("kind", "TELEPORT"), ("status", "MAYBE")])
async def test_unknown_kind_or_status_rejected(ctx, column: str, value: str) -> None:  # type: ignore[no-untyped-def]
    session, user = ctx
    uid = user.id
    action = _action(uid)
    session.add(action)
    await session.commit()
    with pytest.raises(IntegrityError):
        await session.execute(
            text(f"UPDATE position_actions SET {column} = :v WHERE id = :id"),
            {"v": value, "id": action.id},
        )
    await session.rollback()


async def test_order_link_is_set_null_when_action_deleted(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user = ctx
    uid = user.id
    action = _action(uid)
    session.add(action)
    await session.flush()
    order = ExecutionOrder(
        user_id=uid, position_action_id=action.id, client_order_id=f"tm{action.id}u{uid}S",
        symbol="XRP-USDT", side=OrderSide.SELL, position_side=TradeSide.LONG,
        order_type=OrderType.STOP_MARKET, role=OrderRole.STOP_LOSS, status=OrderStatus.CANCELLED,
    )
    session.add(order)
    await session.commit()
    await session.delete(action)
    await session.commit()
    await session.refresh(order)
    assert order.position_action_id is None
    assert order.status is OrderStatus.CANCELLED


@pytest.mark.parametrize("action", list(ExecutionCallbackAction))
async def test_callback_accepts_all_actions(ctx, action: ExecutionCallbackAction) -> None:  # type: ignore[no-untyped-def]
    session, user = ctx
    uid = user.id
    row = ExecutionCallback(user_id=uid, action=action.value, position_action_id=42)
    session.add(row)
    await session.commit()
    assert row.id is not None


async def test_callback_unknown_action_rejected(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user = ctx
    uid = user.id
    session.add(ExecutionCallback(user_id=uid, action="pm_bogus"))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_trade_initial_stop_loss_persists(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user = ctx
    uid = user.id
    trade = await TradeJournal(TradeRepository(session)).open_trade(
        user_id=uid, symbol="XRP-USDT", side=TradeSide.LONG, entry_price=D("1.5253"),
        quantity=D(40), stop_loss=D("1.4795"),
    )
    # Стоп перенесён в безубыток, исходный — база 1R.
    trade.initial_stop_loss = D("1.4795")
    trade.stop_loss = D("1.5253")
    await session.commit()
    await session.refresh(trade)
    assert trade.initial_stop_loss == D("1.4795") and trade.risk_stop == D("1.4795")


def test_both_cancel_spellings_exist() -> None:
    """CANCELED — уже в execution_orders (reconciler 15.6), CANCELLED — BingX."""
    assert OrderStatus.CANCELED.value == "CANCELED"
    assert OrderStatus.CANCELLED.value == "CANCELLED"
