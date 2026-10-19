"""B.9: восстановление действий этапа 4 после рестарта
(app/execution/action_recovery.py) против настоящей БД и фейковой биржи с
состоянием (FakeExchange из test_position_action_service + история и 109421).

Обрыв делает управляемый сбой EXEC_OPEN_FAULT interrupt_action_after_pending /
_after_post (то же, что проверка на демо), прочие состояния — строками
напрямую. Правила владельца 18–19.10: биржа только читается (ни одного POST и
DELETE в восстановлении), клиент — счёта действия, NOT_PLACED — только после
двух отрицательных проверок в разных проходах (не раньше 30 с после первой),
сообщение в чат — итогом; завершённые действия — только строки (#40/210),
кроме исполнившегося закрытия.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from sqlalchemy import select, update

from app.core.config import Settings
from app.core.locks import RedisLock, position_lock_key
from app.database.models.execution_order import ExecutionOrder
from app.database.models.position_action import PositionAction
from app.database.models.trade import Trade
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.trade import TradeRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import ExchangeResponseError, HistoryOrder, OrderFill
from app.execution import action_recovery
from app.execution.action_recovery import (
    MANUAL_RECONCILE,
    NOT_FOUND_KEY,
    RECOVERED_MARK,
    RecoveryNote,
    due_action_ids,
    recover_actions,
)
from app.execution.close_backfill import CONFIRMED_MARK, UNCONFIRMED_MESSAGE
from app.execution.position_action_service import SUBMITTED_TEXT, PositionActionService
from app.market.cache import TTLCache
from app.services.user_service import UserService
from app.trading.enums import (
    ExchangeKeyMode,
    OrderRole,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionActionKind,
    PositionActionStatus,
    TradeSide,
    TradeSource,
)
from app.trading.journal import TradeJournal
from tests.conftest import cleanup_user
from tests.test_position_action_service import BE, ENTRY, OLD_STOP, PID, FakeExchange, _open_order

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

D = Decimal
T0 = datetime.now(UTC)
_SENDS = {"market", "place_conditional", "cancel"}


class RecoveryExchange(FakeExchange):
    """+ история allOrders и честный 109421 на неизвестный cid."""

    def __init__(self) -> None:
        super().__init__()
        self.history: list[HistoryOrder] = []
        self.history_error: Exception | None = None

    async def get_order_fill(self, symbol, client_order_id, *, max_retries=None) -> OrderFill:  # type: ignore[no-untyped-def]
        self.calls.append(("fill", client_order_id))
        if self.fill_errors:
            raise self.fill_errors.pop(0)
        if client_order_id in self.fills:
            return self.fills[client_order_id]
        raise ExchangeResponseError("BingX: order not exist (код 109421)", code=109421)

    async def get_all_orders(self, symbol, start, end, *, max_retries=None) -> list[HistoryOrder]:  # type: ignore[no-untyped-def]
        self.calls.append(("history", (start, end)))
        if self.history_error is not None:
            raise self.history_error
        return list(self.history)


def _history(cid: str | None, status: str = "CANCELLED") -> HistoryOrder:
    return HistoryOrder(
        order_id="2106000000000000001", symbol="XRP-USDT", side="SELL", position_side="LONG",
        order_type="STOP_MARKET", status=status, avg_price=D(0), executed_qty=D(0), fee=D(0),
        realized_pnl=D(0), reduce_only=True, trigger_order_id=None, position_id=PID,
        created_at=T0, updated_at=T0, client_order_id=cid,
    )


@pytest_asyncio.fixture
async def ctx(unique_telegram_id) -> AsyncIterator[SimpleNamespace]:  # type: ignore[no-untyped-def]
    settings = Settings(  # type: ignore[call-arg]
        trading_execution_enabled=True, bingx_trading_mode="demo", exec_dry_run=False,
        exec_order_readback_delay_ms=0,
    )
    db = Database(settings)
    exchange = RecoveryExchange()
    modes: list[Any] = []

    class Factory:
        async def get_credentials(self, session, user_id, exchange="bingx", mode=None):  # type: ignore[no-untyped-def]
            return SimpleNamespace(is_read_only=False, permissions_checked_at=T0,
                                   user_id=user_id, mode=mode)

        async def for_user(self, session, user_id, exchange_name="bingx", mode=None):  # type: ignore[no-untyped-def]
            modes.append(mode)
            return exchange

    redis = FakeRedis()
    notes: list[RecoveryNote] = []

    async def notify(note: RecoveryNote) -> None:
        notes.append(note)

    async with db.session() as session:
        user = await UserService(
            UserRepository(session), StrategyRepository(session),
            MistakeTypeRepository(session), settings,
        ).get_or_create(telegram_id=unique_telegram_id())
        user.settings.active_exchange_mode = ExchangeKeyMode.DEMO
        await session.commit()
        uid = user.id

        def service(**overrides: Any) -> PositionActionService:
            s = settings.model_copy(update=overrides) if overrides else settings
            return PositionActionService(session, s, None, user, redis=redis, factory=Factory(),
                                         market_cache=TTLCache())

        async def recover(at: datetime) -> int:
            return await recover_actions(db, settings, redis, Factory(), notify, now=at)

        c = SimpleNamespace(session=session, db=db, uid=uid, user=user, exchange=exchange,
                            service=service, recover=recover, notes=notes, redis=redis,
                            modes=modes, settings=settings)
        yield c
        await session.rollback()
        fresh = await UserRepository(session).get_by_id(uid)
        await cleanup_user(session, fresh)
        await session.commit()
    await db.dispose()


async def _confirm(c, kind=PositionActionKind.CLOSE_FULL, params=None, fault=""):  # type: ignore[no-untyped-def]
    card = await c.service().open_card(
        kind, params if params is not None else {}, "XRP-USDT", TradeSide.LONG, message_id=55,
    )
    assert card.action is not None, card.text
    result = await c.service(exec_open_fault=fault).confirm(
        card.action.id, message_id=55, risk_confirmed=False,
    )
    await c.session.refresh(card.action)
    return card.action, result.text


async def _rows(c, action_id: int) -> list[ExecutionOrder]:  # type: ignore[no-untyped-def]
    return list(await c.session.scalars(
        select(ExecutionOrder).where(ExecutionOrder.position_action_id == action_id)
        .order_by(ExecutionOrder.id).execution_options(populate_existing=True)
    ))


async def _action(c, action_id: int) -> PositionAction:  # type: ignore[no-untyped-def]
    action = await c.session.get(PositionAction, action_id, populate_existing=True)
    assert action is not None
    return action


def _sends(c, since: int) -> list[str]:  # type: ignore[no-untyped-def]
    return [name for name, _ in c.exchange.calls[since:] if name in _SENDS]


async def _trade(c) -> Trade:  # type: ignore[no-untyped-def]
    trade = await TradeJournal(TradeRepository(c.session)).open_trade(
        user_id=c.uid, symbol="XRP-USDT", side=TradeSide.LONG, entry_price=ENTRY,
        quantity=D(30), stop_loss=D("1.4795"), source=TradeSource.IMPORTED,
        account_mode=ExchangeKeyMode.DEMO,
        external_position_id=PID, external_fill_id="f1", fee=D("0.0229"),
    )
    await c.session.commit()
    return trade


# --- «Да» с B.9: SUBMITTED вместе с PENDING, сбои-обрывы -----------------------------------


async def test_fault_after_pending_leaves_submitted_and_pending(ctx) -> None:  # type: ignore[no-untyped-def]
    action, text = await _confirm(ctx, fault="interrupt_action_after_pending")
    assert "interrupt_action_after_pending" in text
    assert action.status is PositionActionStatus.SUBMITTED
    [row] = await _rows(ctx, action.id)
    assert (row.status, row.exchange_order_id) == (OrderStatus.PENDING, None)
    assert row.account_mode is ExchangeKeyMode.DEMO and action.account_mode is ExchangeKeyMode.DEMO
    assert "market" not in ctx.exchange.names()


async def test_second_yes_on_submitted_does_not_replan(ctx) -> None:  # type: ignore[no-untyped-def]
    """До B.9 повторное «Да» пересчитывало карточку: исполненное закрытие давало
    ложный REFUSED «объём изменился». Теперь — ни одного запроса к бирже."""
    action, _ = await _confirm(ctx, fault="interrupt_action_after_post")
    calls = len(ctx.exchange.calls)
    again = await ctx.service().confirm(action.id, message_id=55, risk_confirmed=False)
    assert again.text == SUBMITTED_TEXT and not again.final   # карточку не переписывать
    assert len(ctx.exchange.calls) == calls
    assert (await _action(ctx, action.id)).status is PositionActionStatus.SUBMITTED


async def test_post_filled_row_price_is_null_until_read(ctx) -> None:  # type: ignore[no-untyped-def]
    """POST FILLED: в закоммиченной строке цена NULL уже ДО дочитки — обрыв в
    этот момент не оставляет mark карточки (его видит отдельная сессия, как
    восстановление после рестарта). Дочитка не удалась — NULL и в итоге."""
    seen: list[tuple[Any, Any]] = []
    read = ctx.exchange.get_order_fill

    async def spy(symbol, cid, *, max_retries=None):  # type: ignore[no-untyped-def]
        if not seen:
            async with ctx.db.session() as other:
                seen.append((await other.execute(
                    select(ExecutionOrder.status, ExecutionOrder.price)
                    .where(ExecutionOrder.client_order_id == cid)
                )).one())
        return await read(symbol, cid, max_retries=max_retries)

    ctx.exchange.get_order_fill = spy
    ctx.exchange.fill_errors = [ExchangeResponseError("сеть", code=None)] * 3
    action, text = await _confirm(ctx)
    assert seen == [(OrderStatus.FILLED, None)]
    [row] = await _rows(ctx, action.id)
    assert row.status is OrderStatus.FILLED and row.price is None
    assert "цена выхода биржа ещё не отдала" in text


# --- закрытие --------------------------------------------------------------------------------


async def test_close_not_sent_not_placed_only_after_two_checks(ctx) -> None:  # type: ignore[no-untyped-def]
    action, _ = await _confirm(ctx, fault="interrupt_action_after_pending")
    since = len(ctx.exchange.calls)

    assert await ctx.recover(T0) == 0                       # первая проверка: отметка
    [row] = await _rows(ctx, action.id)
    assert row.status is OrderStatus.PENDING
    key = NOT_FOUND_KEY.format(row_id=row.id)
    assert await ctx.redis.get(key) is not None
    assert 0 < await ctx.redis.ttl(key) <= 3600                  # не копится: час
    assert await ctx.recover(T0 + timedelta(seconds=10)) == 0   # рано: < 30 с
    assert (await _rows(ctx, action.id))[0].status is OrderStatus.PENDING
    assert ctx.notes == []

    assert await ctx.recover(T0 + timedelta(seconds=40)) == 1   # вторая — NOT_PLACED
    [row] = await _rows(ctx, action.id)
    assert row.status is OrderStatus.NOT_PLACED
    fresh = await _action(ctx, action.id)
    assert fresh.status is PositionActionStatus.FAILED and fresh.error_code == "INTERRUPTED"
    [note] = ctx.notes
    assert note.action_id == action.id and note.card_message_id == 55
    assert "♻️ После перезапуска бота — закрытие XRP-USDT LONG" in note.text
    assert "не было отправлено" in note.text
    assert await ctx.redis.get(NOT_FOUND_KEY.format(row_id=row.id)) is None
    names = [n for n, _ in ctx.exchange.calls[since:]]
    assert names.count("history") == 3 and _sends(ctx, since) == []
    assert ctx.exchange.position is not None and ctx.exchange.position.quantity == D(30)


async def test_close_sent_done_first_pass_journal_once(ctx) -> None:  # type: ignore[no-untyped-def]
    trade = await _trade(ctx)
    action, _ = await _confirm(ctx, fault="interrupt_action_after_post")
    [row] = await _rows(ctx, action.id)
    assert row.status is OrderStatus.PENDING and ctx.exchange.position is None
    since = len(ctx.exchange.calls)

    assert await ctx.recover(T0) == 1
    [row] = await _rows(ctx, action.id)
    assert (row.status, row.price, row.quantity) == (OrderStatus.FILLED, D("1.5600"), D(30))
    assert row.exchange_order_id and row.raw_response["source"] == RECOVERED_MARK
    assert (await _action(ctx, action.id)).status is PositionActionStatus.DONE
    [note] = ctx.notes
    assert "✅ Закрыто 30 по 1.56 · комиссия" in note.text   # как итог «Да» (C.20 — нули шага)
    assert "биржа сняла сама" in note.text
    fresh = await ctx.session.scalar(
        select(Trade).where(Trade.id == trade.id).execution_options(populate_existing=True)
    )
    await ctx.session.refresh(fresh, ["fills"])
    assert {f.external_fill_id for f in fresh.fills} == {"f1", row.exchange_order_id}
    assert not fresh.is_open
    assert _sends(ctx, since) == []

    assert await ctx.recover(T0 + timedelta(minutes=1)) == 0   # больше нечего
    assert len(ctx.notes) == 1


async def test_partial_close_sent_levels_after(ctx) -> None:  # type: ignore[no-untyped-def]
    action, _ = await _confirm(ctx, PositionActionKind.CLOSE_PARTIAL, {"fraction": "25"},
                               fault="interrupt_action_after_post")
    await ctx.recover(T0)
    assert (await _action(ctx, action.id)).status is PositionActionStatus.DONE
    [note] = ctx.notes
    assert "частичное закрытие" in note.text and "Остаток 23" in note.text


async def test_filled_by_post_without_price_done(ctx) -> None:  # type: ignore[no-untyped-def]
    """Обрыв после FILLED по POST до дочитки: строка FILLED без цены."""
    action, _ = await _confirm(ctx, fault="interrupt_action_after_post")
    [row] = await _rows(ctx, action.id)
    row.status, row.price = OrderStatus.FILLED, None
    await ctx.session.commit()
    await ctx.recover(T0)
    [row] = await _rows(ctx, action.id)
    assert row.price == D("1.5600")
    assert (await _action(ctx, action.id)).status is PositionActionStatus.DONE


async def test_full_close_leftovers_reported_not_cancelled(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.exchange.auto_cancel_on_close = False
    await _confirm(ctx, fault="interrupt_action_after_post")
    since = len(ctx.exchange.calls)
    await ctx.recover(T0)
    [note] = ctx.notes
    assert f"остались условные ордера: {OLD_STOP}" in note.text
    assert _sends(ctx, since) == []


# --- завершённые действия (#40, #37) ------------------------------------------------------


async def _finished(c, status, message=None, row_status=OrderStatus.PENDING):  # type: ignore[no-untyped-def]
    action, _ = await _confirm(c, fault="interrupt_action_after_pending")
    action.status = status
    action.error_code = "CARD_EXPIRED" if status is PositionActionStatus.EXPIRED else "X"
    action.error_message = message
    [row] = await _rows(c, action.id)
    row.status = row_status
    await c.session.commit()
    return action, row


async def test_expired_with_pending_row_rows_only_silent(ctx) -> None:  # type: ignore[no-untyped-def]
    """#40/210: «Да» совпало с рестартом, потом карточка истекла. POST не
    уходил — строка NOT_PLACED после двух проверок, действие EXPIRED, в чат ничего."""
    action, row = await _finished(ctx, PositionActionStatus.EXPIRED)
    assert action.id in await due_action_ids(ctx.session)
    await ctx.recover(T0)
    await ctx.recover(T0 + timedelta(seconds=31))
    [row] = await _rows(ctx, action.id)
    assert row.status is OrderStatus.NOT_PLACED
    assert (await _action(ctx, action.id)).status is PositionActionStatus.EXPIRED
    assert ctx.notes == []
    assert action.id not in await due_action_ids(ctx.session)


async def test_expired_but_close_filled_becomes_done_with_note(ctx) -> None:  # type: ignore[no-untyped-def]
    action, row = await _finished(ctx, PositionActionStatus.EXPIRED)
    ctx.exchange.fills[row.client_order_id] = OrderFill(
        "2106099", row.client_order_id, "FILLED", D("1.5500"), D(30), D(30), D("0.02"), {}, T0,
    )
    await ctx.recover(T0)
    fresh = await _action(ctx, action.id)
    assert fresh.status is PositionActionStatus.DONE and fresh.error_code is None
    assert fresh.params["confirmed_by"] == RECOVERED_MARK
    [note] = ctx.notes
    assert (f"действие #{action.id} ранее было отмечено как неудачное, но закрытие исполнилось "
            "на бирже.") in note.text
    assert "✅ Закрыто 30 по 1.55" in note.text


async def test_failed_unconfirmed_done_silently(ctx) -> None:  # type: ignore[no-untyped-def]
    """Правило 12.10 (#37): FAILED «не подтверждено», строка добрана — DONE с
    пометкой «добор по cid», без сообщения."""
    action, row = await _finished(ctx, PositionActionStatus.FAILED, UNCONFIRMED_MESSAGE,
                                  OrderStatus.UNKNOWN)
    ctx.exchange.fills[row.client_order_id] = OrderFill(
        "2106098", row.client_order_id, "FILLED", D("1.5500"), D(30), D(30), D("0.02"), {}, T0,
    )
    await ctx.recover(T0)
    fresh = await _action(ctx, action.id)
    assert fresh.status is PositionActionStatus.DONE and fresh.error_message is None
    assert fresh.params["confirmed_by"] == CONFIRMED_MARK
    assert ctx.notes == []


async def test_done_action_with_standing_stop_not_selected(ctx) -> None:  # type: ignore[no-untyped-def]
    """Строка 75 прода: стоп DONE-действия FILLED без цены, и стоящий стоп
    SUBMITTED — норма, восстановлению не нужны; CARD без строк — тоже."""
    action, _ = await _confirm(ctx, PositionActionKind.MOVE_STOP, {"breakeven": True})
    assert action.status is PositionActionStatus.DONE
    card = await ctx.service().open_card(
        PositionActionKind.CLOSE_FULL, {}, "XRP-USDT", TradeSide.LONG, message_id=56,
    )
    ids = await due_action_ids(ctx.session)
    assert action.id not in ids and card.action is not None and card.action.id not in ids


# --- перенос стопа ---------------------------------------------------------------------------


async def test_move_interrupted_after_bridge_two_stops(ctx) -> None:  # type: ignore[no-untyped-def]
    action, _ = await _confirm(ctx, PositionActionKind.MOVE_STOP, {"breakeven": True},
                               fault="interrupt_action_after_post")
    [row] = await _rows(ctx, action.id)
    assert row.client_order_id == f"tm{action.id}u{ctx.uid}SB" and row.status is OrderStatus.PENDING
    since = len(ctx.exchange.calls)
    await ctx.recover(T0)
    [row] = await _rows(ctx, action.id)
    assert row.status is OrderStatus.SUBMITTED and row.exchange_order_id
    fresh = await _action(ctx, action.id)
    assert fresh.status is PositionActionStatus.FAILED and fresh.error_code == "INTERRUPTED"
    [note] = ctx.notes
    assert "перенос стопа" in note.text and "старый стоп 1.4795" in note.text
    assert "два стопа" in note.text and "Сними лишний в BingX" in note.text
    assert _sends(ctx, since) == []
    assert len(ctx.exchange.orders) == 2                       # ничего не снято


async def _move_state(c, *, final: bool, bridge: bool, old: bool):  # type: ignore[no-untyped-def]
    """SUBMITTED-перенос: строки моста и итога PENDING, на бирже — по флагам."""
    action, _ = await _confirm(c, PositionActionKind.MOVE_STOP, {"breakeven": True},
                               fault="interrupt_action_after_pending")
    [bridge_row] = await _rows(c, action.id)
    c.session.add(ExecutionOrder(
        account_mode=ExchangeKeyMode.DEMO, user_id=c.uid, position_action_id=action.id,
        client_order_id=f"tm{action.id}u{c.uid}S", symbol="XRP-USDT", side=OrderSide.SELL,
        position_side=TradeSide.LONG, order_type=OrderType.STOP_MARKET,
        role=OrderRole.STOP_LOSS, quantity=D(30), trigger_price=BE, status=OrderStatus.PENDING,
    ))
    if final and not bridge:
        bridge_row.status = OrderStatus.CANCELLED   # снят и подтверждён до обрыва
    await c.session.commit()
    orders = []
    if old:
        orders.append(_open_order(OLD_STOP, "1.4795"))
    if bridge:
        orders.append(_open_order("2105990000000000001", str(BE), cid=bridge_row.client_order_id
                                  .lower(), close_position=False))
    if final:
        orders.append(_open_order("2105990000000000002", str(BE),
                                  cid=f"tm{action.id}u{c.uid}s"))
    c.exchange.orders = orders
    return action


@pytest.mark.parametrize(("final", "bridge", "old", "status", "words"), [
    (True, False, False, PositionActionStatus.DONE, "✅ Стоп 1.4795 → "),
    (True, True, False, PositionActionStatus.FAILED, "не снят — на позиции два стопа"),
    (True, False, True, PositionActionStatus.FAILED, "Сними старый в BingX"),
    (False, True, False, PositionActionStatus.FAILED, "стоит промежуточным ордером на 40"),
])
async def test_move_outcome_by_what_stands(ctx, final, bridge, old, status, words) -> None:  # type: ignore[no-untyped-def]
    action = await _move_state(ctx, final=final, bridge=bridge, old=old)
    since = len(ctx.exchange.calls)
    await ctx.recover(T0)
    await ctx.recover(T0 + timedelta(seconds=31))   # итог без моста — после двух проверок
    fresh = await _action(ctx, action.id)
    assert fresh.status is status
    [note] = ctx.notes
    assert words in note.text, note.text
    assert _sends(ctx, since) == []


async def test_move_nothing_sent_old_stands(ctx) -> None:  # type: ignore[no-untyped-def]
    """Обрыв до POST моста: две проверки «нет» по каждой строке — «всё как было»."""
    action = await _move_state(ctx, final=False, bridge=False, old=True)
    await ctx.recover(T0)
    assert ctx.notes == []
    await ctx.recover(T0 + timedelta(seconds=31))
    rows = await _rows(ctx, action.id)
    assert {r.status for r in rows} == {OrderStatus.NOT_PLACED}
    [note] = ctx.notes
    assert "всё как было: стоп 1.4795" in note.text


async def test_move_no_stop_left_alarm(ctx) -> None:  # type: ignore[no-untyped-def]
    action = await _move_state(ctx, final=False, bridge=False, old=False)
    await ctx.recover(T0)
    await ctx.recover(T0 + timedelta(seconds=31))
    [note] = ctx.notes
    assert "🚨 на позиции нет стопа" in note.text
    assert (await _action(ctx, action.id)).status is PositionActionStatus.FAILED


# --- доказательства и отказ от вывода ---------------------------------------------------


async def test_exchange_error_changes_nothing(ctx) -> None:  # type: ignore[no-untyped-def]
    action, _ = await _confirm(ctx, fault="interrupt_action_after_pending")
    ctx.exchange.fill_errors = [ExchangeResponseError("сеть", code=None)]
    assert await ctx.recover(T0) == 0
    [row] = await _rows(ctx, action.id)
    assert row.status is OrderStatus.PENDING
    assert await ctx.redis.get(NOT_FOUND_KEY.format(row_id=row.id)) is None


@pytest.mark.parametrize("history", ["cid", "no_field", "error"])
async def test_history_not_proving_absence_never_not_placed(ctx, history) -> None:  # type: ignore[no-untyped-def]
    action, _ = await _confirm(ctx, fault="interrupt_action_after_pending")
    [row] = await _rows(ctx, action.id)
    if history == "cid":
        ctx.exchange.history = [_history(row.client_order_id.lower())]
    elif history == "no_field":
        ctx.exchange.history = [_history(None)]
    else:
        ctx.exchange.history_error = ExchangeResponseError("сеть", code=None)
    for seconds in (0, 40, 80):
        await ctx.recover(T0 + timedelta(seconds=seconds))
    [row] = await _rows(ctx, action.id)
    assert row.status is OrderStatus.PENDING
    assert (await _action(ctx, action.id)).status is PositionActionStatus.SUBMITTED
    assert ctx.notes == []


async def test_order_seen_between_checks_clears_mark(ctx) -> None:  # type: ignore[no-untyped-def]
    action, _ = await _confirm(ctx, fault="interrupt_action_after_pending")
    [row] = await _rows(ctx, action.id)
    await ctx.recover(T0)
    assert await ctx.redis.get(NOT_FOUND_KEY.format(row_id=row.id)) is not None
    ctx.exchange.fills[row.client_order_id] = OrderFill(
        "2106097", row.client_order_id, "NEW", D(0), D(30), D(0), D(0), {}, None,
    )
    await ctx.recover(T0 + timedelta(seconds=40))
    assert await ctx.redis.get(NOT_FOUND_KEY.format(row_id=row.id)) is None
    assert (await _rows(ctx, action.id))[0].status is OrderStatus.PENDING


async def test_lock_busy_skips(ctx) -> None:  # type: ignore[no-untyped-def]
    action, _ = await _confirm(ctx, fault="interrupt_action_after_post")
    since = len(ctx.exchange.calls)
    async with RedisLock(ctx.redis, position_lock_key(ctx.uid, "XRP-USDT", "LONG"), 30):
        assert await ctx.recover(T0) == 0
    assert ctx.exchange.calls[since:] == []
    assert (await _action(ctx, action.id)).status is PositionActionStatus.SUBMITTED


async def test_client_of_action_account(ctx) -> None:  # type: ignore[no-untyped-def]
    """Клиент — счёта действия (M9), не текущего режима настроек."""
    action, _ = await _confirm(ctx, fault="interrupt_action_after_post")
    action.account_mode = ExchangeKeyMode.LIVE
    await ctx.session.commit()
    ctx.modes.clear()
    await ctx.recover(T0)
    assert ctx.modes == [ExchangeKeyMode.LIVE]


async def test_submitted_without_rows_failed(ctx) -> None:  # type: ignore[no-untyped-def]
    card = await ctx.service().open_card(
        PositionActionKind.CLOSE_FULL, {}, "XRP-USDT", TradeSide.LONG, message_id=55,
    )
    assert card.action is not None
    card.action.status = PositionActionStatus.SUBMITTED
    await ctx.session.commit()
    await ctx.recover(T0)
    assert (await _action(ctx, card.action.id)).status is PositionActionStatus.FAILED
    [note] = ctx.notes
    assert "до отправки" in note.text


async def test_one_action_failure_does_not_stop_others(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    first, _ = await _confirm(ctx, fault="interrupt_action_after_post")
    real = action_recovery.recover_one
    calls: list[int] = []

    async def flaky(db, settings, redis, factory, notify, action_id, *, now=None):  # type: ignore[no-untyped-def]
        calls.append(action_id)
        if action_id == first.id:
            raise RuntimeError("boom")
        return await real(db, settings, redis, factory, notify, action_id, now=now)

    monkeypatch.setattr(action_recovery, "recover_one", flaky)
    ctx.exchange.position = FakeExchange().position       # позиция снова есть
    second, _ = await _confirm(ctx, fault="interrupt_action_after_post")
    assert await ctx.recover(T0) == 1
    assert calls == [first.id, second.id]
    assert (await _action(ctx, second.id)).status is PositionActionStatus.DONE


# --- строки старше окна истории BingX (владелец 19.10) ------------------------------------


async def _age(c, row_id: int, days: int = 8) -> None:  # type: ignore[no-untyped-def]
    await c.session.execute(update(ExecutionOrder).where(ExecutionOrder.id == row_id)
                            .values(created_at=T0 - timedelta(days=days)))
    await c.session.commit()


async def test_old_row_one_check_then_manual_and_excluded(ctx, caplog) -> None:  # type: ignore[no-untyped-def]
    """#40/210: строка старше окна истории — одна проверка по cid (без истории),
    WARNING «нужна ручная сверка», дальше циклы её не берут."""
    import logging

    action, row = await _finished(ctx, PositionActionStatus.EXPIRED)
    await _age(ctx, row.id)
    since = len(ctx.exchange.calls)
    with caplog.at_level(logging.WARNING):
        await ctx.recover(T0)
    names = [n for n, _ in ctx.exchange.calls[since:]]
    assert names.count("fill") == 1 and "history" not in names
    [rec] = [r for r in caplog.records
             if r.getMessage() == "Строка действия старше окна истории BingX — нужна ручная сверка"]
    assert rec.levelno == logging.WARNING
    [row] = await _rows(ctx, action.id)
    assert row.status is OrderStatus.PENDING and row.error_code == MANUAL_RECONCILE
    assert "нужна ручная сверка" in row.error_message
    assert (await _action(ctx, action.id)).status is PositionActionStatus.EXPIRED
    assert action.id not in await due_action_ids(ctx.session)
    since = len(ctx.exchange.calls)
    await ctx.recover(T0 + timedelta(minutes=5))
    assert ctx.exchange.calls[since:] == [] and ctx.notes == []


async def test_old_row_filled_still_resolved(ctx) -> None:  # type: ignore[no-untyped-def]
    """Старое, но по cid исполнено — это доказательство: DONE, как обычно."""
    action, row = await _finished(ctx, PositionActionStatus.EXPIRED)
    await _age(ctx, row.id)
    ctx.exchange.fills[row.client_order_id] = OrderFill(
        "2106096", row.client_order_id, "FILLED", D("1.5500"), D(30), D(30), D("0.02"), {}, T0,
    )
    await ctx.recover(T0)
    assert (await _action(ctx, action.id)).status is PositionActionStatus.DONE
    assert (await _rows(ctx, action.id))[0].error_code != MANUAL_RECONCILE


async def test_old_row_of_active_action_excludes_action(ctx) -> None:  # type: ignore[no-untyped-def]
    action, _ = await _confirm(ctx, fault="interrupt_action_after_pending")
    [row] = await _rows(ctx, action.id)
    await _age(ctx, row.id)
    await ctx.recover(T0)
    fresh = await _action(ctx, action.id)
    assert fresh.status is PositionActionStatus.SUBMITTED
    assert fresh.error_code == MANUAL_RECONCILE
    assert action.id not in await due_action_ids(ctx.session)
    assert ctx.notes == []
