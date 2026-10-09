"""A.3 (10.10.2026): стоп/тейк-строки закрытой позиции — FILLED/CANCELLED
только по факту биржи (app/execution/settle.py).

Правило: ордер есть в openOrders — строку не трогать; выбор строк — по
сделке, открытию, positionId, не по символу; REJECTED с orderId — статус по
факту, error_code остаётся. Плюс проверка кода: CANCELED (одна L) никто не
пишет."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from app.core.config import Settings
from app.database.models.execution_order import ExecutionOrder
from app.database.models.position_action import PositionAction
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import HistoryOrder
from app.execution.settle import conditional_rows, history_facts, settle
from app.services.user_service import UserService
from app.trading.enums import (
    CancelSource,
    OrderRole,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionActionKind,
    PositionActionStatus,
    TradeSide,
)
from tests.conftest import cleanup_user

D = Decimal
ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def _row(oid: str | None, status: OrderStatus = OrderStatus.SUBMITTED, *,
         role: OrderRole = OrderRole.STOP_LOSS, **kw: Any) -> ExecutionOrder:
    return ExecutionOrder(
        user_id=kw.pop("user_id", 1), exchange_order_id=oid, symbol="XRP-USDT",
        side=OrderSide.SELL, position_side=TradeSide.LONG,
        order_type=(OrderType.STOP_MARKET if role is OrderRole.STOP_LOSS
                    else OrderType.TAKE_PROFIT_MARKET if role is OrderRole.TAKE_PROFIT
                    else OrderType.MARKET),
        role=role, status=status, **kw,
    )


def _hist(oid: str, status: str, trigger: str | None = None) -> HistoryOrder:
    return HistoryOrder(
        order_id=oid, symbol="XRP-USDT", side="SELL", position_side="LONG",
        order_type="STOP_MARKET", status=status, avg_price=D(0), executed_qty=D(0), fee=D(0),
        realized_pnl=D(0), reduce_only=True, trigger_order_id=trigger, position_id=None,
        created_at=NOW, updated_at=NOW,
    )


# --- settle: чистая логика -------------------------------------------------------------


def test_standing_order_is_never_touched() -> None:
    gone, live = _row("1"), _row("2")
    gone.id, live.id = 10, 11
    result = settle([gone, live], open_order_ids={"2"})
    assert (gone.status, gone.cancel_source) == (OrderStatus.CANCELLED, CancelSource.EXCHANGE)
    assert live.status is OrderStatus.SUBMITTED and live.cancel_source is None
    assert result.settled == [10] and result.standing == [11]


def test_facts_decide_status_filled_has_no_source() -> None:
    fired, taken = _row("1"), _row("2", role=OrderRole.TAKE_PROFIT)
    fired.id, taken.id = 1, 2
    settle([fired, taken], open_order_ids=set(),
           facts={"1": OrderStatus.FILLED, "2": OrderStatus.CANCELLED})
    assert fired.status is OrderStatus.FILLED and fired.cancel_source is None
    assert taken.status is OrderStatus.CANCELLED and taken.cancel_source is CancelSource.EXCHANGE


def test_default_none_leaves_row_undecided() -> None:
    row = _row("1")
    row.id = 5
    result = settle([row], open_order_ids=set(), facts={}, default=None)
    assert row.status is OrderStatus.SUBMITTED and result.undecided == [5]


def test_rejected_with_order_id_settles_and_keeps_error() -> None:
    """Т4: стоп встал, но «по cid не найден» — REJECTED с orderId. Биржа его
    приняла — статус по факту, решение «не найден» остаётся историей."""
    row = _row("1", OrderStatus.REJECTED, error_code="NOT_FOUND", error_message="не найден")
    row.id = 1
    settle([row], open_order_ids=set())
    assert row.status is OrderStatus.CANCELLED and row.cancel_source is CancelSource.EXCHANGE
    assert row.error_code == "NOT_FOUND" and row.error_message == "не найден"


def test_rejected_skipped_when_excluded() -> None:
    row = _row("1", OrderStatus.REJECTED, error_code="NOT_FOUND")
    row.id = 1
    assert settle([row], open_order_ids=set(), include_rejected=False).settled == []
    assert row.status is OrderStatus.REJECTED


@pytest.mark.parametrize("row", [
    _row(None),                                              # без orderId — не знаем, был ли
    _row("1", OrderStatus.FILLED),
    _row("1", OrderStatus.CANCELLED),
    _row("1", OrderStatus.PENDING),
    _row("1", role=OrderRole.ENTRY),
    _row("1", role=OrderRole.CLOSE),
])
def test_only_open_conditionals_with_order_id(row: ExecutionOrder) -> None:
    before = row.status
    assert settle([row], open_order_ids=set()).settled == []
    assert row.status is before


def test_history_facts_trigger_and_filled_wins() -> None:
    facts = history_facts([
        _hist("10", "CANCELLED"),
        _hist("20", "CANCELLED", trigger="20"),       # живьём: triggerOrderId = свой id
        _hist("30", "FILLED", trigger="20"),          # дочерний сработавшего 20
        _hist("40", "EXPIRED"),
        _hist("50", "NEW"),
    ])
    assert facts == {
        "10": OrderStatus.CANCELLED, "20": OrderStatus.FILLED, "30": OrderStatus.FILLED,
        "40": OrderStatus.CANCELLED,
    }


def test_no_code_writes_canceled_one_l() -> None:
    """CANCELED (одна L) только для чтения строк до M7 — запись в коде запрещена."""
    hits = []
    for path in (ROOT / "app").rglob("*.py"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"OrderStatus\.CANCELED\b", line):
                hits.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    assert hits == []


# --- conditional_rows: выбор строк в базе ------------------------------------------------

db_only = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")


@pytest_asyncio.fixture
async def db_ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    async with db.session() as session:
        svc = UserService(UserRepository(session), StrategyRepository(session),
                          MistakeTypeRepository(session), settings)
        user = await svc.get_or_create(telegram_id=unique_telegram_id())
        await session.commit()
        uid = user.id
        yield session, uid
        await session.rollback()
        fresh = await UserRepository(session).get_by_id(uid)
        await cleanup_user(session, fresh)
        await session.commit()
    await db.dispose()


async def _action(session, uid: int, position_id: str) -> int:  # type: ignore[no-untyped-def]
    action = PositionAction(
        user_id=uid, symbol="XRP-USDT", side=TradeSide.LONG, position_id=position_id,
        kind=PositionActionKind.MOVE_STOP, status=PositionActionStatus.DONE,
    )
    session.add(action)
    await session.flush()
    return action.id


@db_only
async def test_rows_by_position_id_not_by_symbol(db_ctx) -> None:  # type: ignore[no-untyped-def]
    """Ручная позиция этапа 4: строки — через positionId её действий. Та же
    монета и сторона другой позиции (другой счёт) не задеваются."""
    session, uid = db_ctx
    mine = await _action(session, uid, "P-1")
    other = await _action(session, uid, "P-2")
    session.add_all([
        _row("101", user_id=uid, position_action_id=mine),
        _row("102", OrderStatus.REJECTED, user_id=uid, position_action_id=mine),
        _row("103", user_id=uid, position_action_id=other),
        _row("104", OrderStatus.CANCELLED, user_id=uid, position_action_id=mine),
        _row(None, OrderStatus.UNKNOWN, user_id=uid, position_action_id=mine),
    ])
    await session.flush()
    rows = await conditional_rows(session, user_id=uid, position_id="P-1")
    assert [r.exchange_order_id for r in rows] == ["101", "102"]
    rows = await conditional_rows(session, user_id=uid, position_id="P-1", include_rejected=False)
    assert [r.exchange_order_id for r in rows] == ["101"]
    assert await conditional_rows(session, user_id=uid) == []      # без ключа — ничего
