"""Добор строк закрытия по cid (деплой 4, 4.4, app/execution/close_backfill.py)
против настоящей БД: UNKNOWN и FILLED без цены → FILLED с ценой; действие
FAILED «не подтверждено» → DONE с пометкой (правило владельца 12.10, #37);
неполный ответ и 109421 — строка не тронута, причина в логе; строк нет — ни
одного GET."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import update

from app.core.config import Settings
from app.database.models.execution_order import ExecutionOrder
from app.database.models.position_action import PositionAction
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import ExchangeResponseError, OrderFill, ReadbackIncomplete
from app.execution.close_backfill import (
    BACKFILL_MAX_ROWS,
    UNCONFIRMED_MESSAGE,
    backfill_close_rows,
)
from app.services.user_service import UserService
from app.trading.enums import (
    OrderRole,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionActionKind,
    PositionActionStatus,
    TradeSide,
)
from tests.conftest import cleanup_user

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

D = Decimal
NOW = datetime.now(UTC)


class Client:
    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    async def get_order_fill(self, symbol: str, cid: str, *, max_retries: int | None = None) -> Any:
        self.calls.append(cid)
        answer = self.answers[cid]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _fill(cid: str, status: str = "FILLED") -> OrderFill:
    return OrderFill(
        order_id="2109373768501567488", client_order_id=cid, status=status,
        avg_price=D("1.4094"), orig_qty=D(194), executed_qty=D(194) if status == "FILLED" else D(0),
        fee=D("0.136712"), raw={}, filled_at=NOW,
    )


@pytest_asyncio.fixture
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    settings = Settings(trading_execution_enabled=True)  # type: ignore[call-arg]
    db = Database(settings)
    async with db.session() as session:
        user = await UserService(
            UserRepository(session), StrategyRepository(session),
            MistakeTypeRepository(session), settings,
        ).get_or_create(telegram_id=unique_telegram_id())
        await session.commit()
        uid = user.id
        yield SimpleNamespace(
            session=session, uid=uid, cid=lambda n, tail="C": f"tm{n}u{uid}{tail}",
        )
        await session.rollback()
        fresh = await UserRepository(session).get_by_id(uid)
        await cleanup_user(session, fresh)
        await session.commit()
    await db.dispose()


async def _action(c, status=PositionActionStatus.FAILED, message=UNCONFIRMED_MESSAGE):  # type: ignore[no-untyped-def]
    action = PositionAction(
        user_id=c.uid, symbol="XRP-USDT", side=TradeSide.LONG, kind=PositionActionKind.CLOSE_FULL,
        status=status, params={}, error_code="POSITION_CHANGED" if message else None,
        error_message=message,
    )
    c.session.add(action)
    await c.session.flush()
    return action


async def _row(c, cid: str, status: OrderStatus, *, action=None, price=None,  # type: ignore[no-untyped-def]
               role=OrderRole.CLOSE) -> ExecutionOrder:
    row = ExecutionOrder(
        user_id=c.uid, position_action_id=action.id if action else None, client_order_id=cid,
        exchange_order_id="2109373768501567488", symbol="XRP-USDT", side=OrderSide.SELL,
        position_side=TradeSide.LONG, order_type=OrderType.MARKET, role=role,
        quantity=D(194), price=price, status=status,
    )
    c.session.add(row)
    await c.session.commit()
    return row


async def test_unknown_row_and_failed_action_confirmed(ctx, caplog) -> None:  # type: ignore[no-untyped-def]
    """#37: строка UNKNOWN, действие FAILED «не подтверждено» → FILLED с ценой
    и комиссией, действие DONE, ошибка очищена, пометка в params."""
    action = await _action(ctx)
    row = await _row(ctx, ctx.cid(37), OrderStatus.UNKNOWN, action=action)
    client = Client({ctx.cid(37): _fill(ctx.cid(37))})
    with caplog.at_level(logging.INFO):
        done = await backfill_close_rows(ctx.session, client, ctx.uid, NOW)  # type: ignore[arg-type]
    await ctx.session.commit()
    assert done == [row.id]
    await ctx.session.refresh(row)
    await ctx.session.refresh(action)
    assert (row.status, row.price, row.quantity) == (OrderStatus.FILLED, D("1.4094"), D(194))
    assert row.raw_response["commission"] == "0.136712"
    assert action.status is PositionActionStatus.DONE
    assert action.error_code is None and action.error_message is None
    assert action.params["confirmed_by"] == "добор по cid"
    messages = [r.getMessage() for r in caplog.records]
    assert "Строка закрытия добрана по cid" in messages
    assert "Действие подтверждено добором" in messages

    client.calls.clear()                       # добрано — следующий проход не читает
    assert await backfill_close_rows(ctx.session, client, ctx.uid, NOW) == []  # type: ignore[arg-type]
    assert client.calls == []


async def test_filled_without_price_backfilled(ctx) -> None:  # type: ignore[no-untyped-def]
    """4.2: закрыто по ответу POST, цена не дочитана — добор цены; действие
    уже DONE — не трогаем."""
    action = await _action(ctx, PositionActionStatus.DONE, None)
    row = await _row(ctx, ctx.cid(38), OrderStatus.FILLED, action=action)
    await backfill_close_rows(ctx.session, Client({ctx.cid(38): _fill(ctx.cid(38))}), ctx.uid, NOW)  # type: ignore[arg-type]
    await ctx.session.commit()
    await ctx.session.refresh(row)
    await ctx.session.refresh(action)
    assert row.price == D("1.4094") and action.status is PositionActionStatus.DONE
    assert "confirmed_by" not in (action.params or {})


async def test_failed_action_other_reason_not_touched(ctx) -> None:  # type: ignore[no-untyped-def]
    """Правило — только для «не подтверждено»: отказ биржи остаётся FAILED."""
    action = await _action(ctx, message="Закрытие не прошло: rejected")
    await _row(ctx, ctx.cid(39), OrderStatus.UNKNOWN, action=action)
    await backfill_close_rows(ctx.session, Client({ctx.cid(39): _fill(ctx.cid(39))}), ctx.uid, NOW)  # type: ignore[arg-type]
    await ctx.session.commit()
    await ctx.session.refresh(action)
    assert action.status is PositionActionStatus.FAILED


@pytest.mark.parametrize("answer", [
    ReadbackIncomplete("avgPrice", payload={"status": "FILLED"}),
    ExchangeResponseError("BingX: order not exist (код 109421)", code=109421),
    "NEW",
])
async def test_unread_row_untouched_reason_logged(ctx, caplog, answer) -> None:  # type: ignore[no-untyped-def]
    action = await _action(ctx)
    row = await _row(ctx, ctx.cid(40), OrderStatus.UNKNOWN, action=action)
    reply = _fill(ctx.cid(40), "NEW") if answer == "NEW" else answer
    with caplog.at_level(logging.INFO):
        done = await backfill_close_rows(ctx.session, Client({ctx.cid(40): reply}), ctx.uid, NOW)  # type: ignore[arg-type]
    await ctx.session.commit()
    assert done == []
    await ctx.session.refresh(row)
    await ctx.session.refresh(action)
    assert row.status is OrderStatus.UNKNOWN and action.status is PositionActionStatus.FAILED
    [rec] = [r for r in caplog.records if r.getMessage() == "Строка закрытия не добрана по cid"]
    assert rec.cid == ctx.cid(40)  # type: ignore[attr-defined]


async def test_nothing_to_backfill_no_requests(ctx) -> None:  # type: ignore[no-untyped-def]
    """Строки с ценой, не CLOSE и старше окна — не берутся, на биржу ни запроса."""
    await _row(ctx, ctx.cid(41), OrderStatus.FILLED, price=D("1.40"))
    await _row(ctx, f"to41u{ctx.uid}s1", OrderStatus.UNKNOWN, role=OrderRole.STOP_LOSS)
    old = await _row(ctx, ctx.cid(42), OrderStatus.UNKNOWN)
    await ctx.session.execute(update(ExecutionOrder).where(ExecutionOrder.id == old.id)
                              .values(created_at=NOW - timedelta(days=8)))
    await ctx.session.commit()
    client = Client({})
    assert await backfill_close_rows(ctx.session, client, ctx.uid, NOW) == []  # type: ignore[arg-type]
    assert client.calls == []


async def test_rows_per_pass_capped(ctx) -> None:  # type: ignore[no-untyped-def]
    cids = [ctx.cid(50 + i) for i in range(BACKFILL_MAX_ROWS + 2)]
    for cid in cids:
        await _row(ctx, cid, OrderStatus.UNKNOWN)
    client = Client({cid: _fill(cid) for cid in cids})
    done = await backfill_close_rows(ctx.session, client, ctx.uid, NOW)  # type: ignore[arg-type]
    assert len(done) == len(client.calls) == BACKFILL_MAX_ROWS
