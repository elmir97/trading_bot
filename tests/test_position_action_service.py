"""Действия с позицией (этап 4, app/execution/position_action_service.py)
против настоящей БД и фейковой биржи с состоянием: карточка, сухой прогон,
перенос стопа/тейка в четыре шага (мост с quantity → отмена старого →
closePosition → отмена моста; фейк, как BingX, не принимает второй
closePosition-ордер того же типа — 110406/110407) и сбой на каждом шаге,
закрытие (маркет → get_order_fill → журнал), рост риска, срок карточки,
повторное «Да», лок, расхождение позиции, идемпотентность по client_order_id.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from sqlalchemy import select

from app.core.config import Settings
from app.core.locks import RedisLock, position_lock_key
from app.database.models.execution_order import ExecutionOrder
from app.database.models.position_action import PositionAction
from app.database.models.trade import Trade
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.trade import TradeRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import (
    Balance,
    CancelResult,
    ExchangeResponseError,
    OpenOrder,
    OrderFill,
    OrderResult,
    Position,
    SymbolInfo,
)
from app.execution.position_action_service import PositionActionService
from app.execution.position_actions import breakeven_price
from app.market.cache import TTLCache
from app.services.user_service import UserService
from app.trading.enums import (
    CancelSource,
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

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

D = Decimal
NOW = datetime.now(UTC)
PID = "2105907655281221634"
OLD_STOP = "2105910661355233280"
ENTRY = D("1.5253")
BE = breakeven_price(TradeSide.LONG, ENTRY, D("0.0005"), ENTRY * D("0.0005"), 4)


def _open_order(oid: str, stop: str, otype: str = "STOP_MARKET", cid: str = "",
                close_position: bool = True) -> OpenOrder:
    return OpenOrder(
        order_id=oid, client_order_id=cid, symbol="XRP-USDT", side="SELL", position_side="LONG",
        order_type=otype, quantity=D(40), executed_qty=D(0), price=D(0), stop_price=D(stop),
        status="NEW", leverage=20, reduce_only=True, close_position=close_position,
        working_type="MARK_PRICE", created_at=NOW, updated_at=NOW, take_profit=None,
        stop_loss=None,
    )


# Второй closePosition-ордер того же типа на позицию BingX не принимает
# (живьём 02.10 12:23, документация docs-v3).
_ALREADY_EXISTS = {"STOP_MARKET": 110406, "TAKE_PROFIT_MARKET": 110407}


class FakeExchange:
    name = "bingx"

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.position: Position | None = Position(
            symbol="XRP-USDT", side=TradeSide.LONG, quantity=D(30), entry_price=ENTRY,
            mark_price=D("1.56"), leverage=20, unrealized_pnl=D("1"),
            liquidation_price=D("1.4553"), position_id=PID,
        )
        self.orders: list[OpenOrder] = [_open_order(OLD_STOP, "1.4795")]
        self.mark = D("1.56")
        self.fail_place = False
        self.drop_new = False
        self.cancel_noop = False
        # Сбой только на N-м вызове (с 1): шаг схемы переноса.
        self.fail_place_at: set[int] = set()
        self.drop_new_at: set[int] = set()
        self.cancel_noop_at: set[int] = set()
        self._places = 0
        self._cancels = 0
        self.auto_cancel_on_close = True
        self._seq = 0
        self.fills: dict[str, OrderFill] = {}

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    async def get_positions(self, *, max_retries=None) -> list[Position]:  # type: ignore[no-untyped-def]
        self.calls.append(("positions", None))
        return [self.position] if self.position else []

    async def get_open_orders(self, symbol=None, *, max_retries=None) -> list[OpenOrder]:  # type: ignore[no-untyped-def]
        self.calls.append(("open_orders", symbol))
        return list(self.orders)

    async def get_mark_price(self, symbol: str) -> Decimal:
        return self.mark

    async def get_symbols(self, *, max_retries=None) -> list[SymbolInfo]:  # type: ignore[no-untyped-def]
        return [SymbolInfo("XRP-USDT", 4, 0, D(2), D(2))]

    async def get_balance(self, *, max_retries=None) -> Balance:  # type: ignore[no-untyped-def]
        return Balance("USDT", D(9000), D(100), D(0), D(10000))

    async def get_position_mode(self, *, max_retries=None) -> bool:  # type: ignore[no-untyped-def]
        return True

    async def place_conditional_order(self, **kw: Any) -> OrderResult:
        self.calls.append(("place_conditional", kw))
        self._places += 1
        if self.fail_place or self._places in self.fail_place_at:
            raise ExchangeResponseError("BingX: rejected (код 109400)", code=109400, payload={})
        close_position = kw.get("close_position", True)
        otype = kw["order_type"]
        if close_position and any(o.order_type == otype and o.close_position for o in self.orders):
            code = _ALREADY_EXISTS[otype]
            raise ExchangeResponseError(
                f"BingX: Position SL order already exists (код {code})", code=code, payload={}
            )
        self._seq += 1
        oid = f"21059{self._seq:014d}"
        if not (self.drop_new or self._places in self.drop_new_at):
            self.orders.append(_open_order(
                oid, str(kw["stop_price"]), otype, kw["client_order_id"].lower(), close_position,
            ))
        return OrderResult(oid, kw["client_order_id"], kw["symbol"], kw["side"].value,
                           kw["position_side"], kw["order_type"], "NEW", {})

    async def cancel_order(self, symbol: str, order_id: str) -> CancelResult:
        self.calls.append(("cancel", order_id))
        self._cancels += 1
        if not (self.cancel_noop or self._cancels in self.cancel_noop_at):
            self.orders = [o for o in self.orders if o.order_id != order_id]
        return CancelResult(order_id, "CANCELLED", {"type": "LIMIT", "stopPrice": ""})

    async def place_market_order(self, **kw: Any) -> OrderResult:
        self.calls.append(("market", kw))
        self._seq += 1
        oid = f"21060{self._seq:014d}"
        assert self.position is not None
        left = self.position.quantity - kw["quantity"]
        self.position = replace(self.position, quantity=left) if left > 0 else None
        if self.position is None and self.auto_cancel_on_close:
            self.orders = []
        self.fills[kw["client_order_id"]] = OrderFill(
            oid, kw["client_order_id"], "FILLED", D("1.5600"), kw["quantity"], kw["quantity"],
            D("0.0234"), {}, NOW,
        )
        return OrderResult(oid, kw["client_order_id"], kw["symbol"], kw["side"].value,
                           kw["position_side"], "MARKET", "FILLED", {})

    async def get_order_fill(self, symbol, client_order_id, *, max_retries=None) -> OrderFill:  # type: ignore[no-untyped-def]
        self.calls.append(("fill", client_order_id))
        return self.fills[client_order_id]

    async def close(self) -> None: ...


@pytest_asyncio.fixture
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    settings = Settings(  # type: ignore[call-arg]
        trading_execution_enabled=True, bingx_trading_mode="demo", exec_dry_run=False,
        exec_order_readback_delay_ms=0,
    )
    db = Database(settings)
    exchange = FakeExchange()
    async with db.session() as session:
        user = await UserService(
            UserRepository(session), StrategyRepository(session),
            MistakeTypeRepository(session), settings,
        ).get_or_create(telegram_id=unique_telegram_id())
        user.settings.active_exchange_mode = ExchangeKeyMode.DEMO
        await session.commit()
        uid = user.id

        class Factory:
            async def get_credentials(self, session, user_id, exchange="bingx", mode=None):  # type: ignore[no-untyped-def]
                return SimpleNamespace(is_read_only=False, permissions_checked_at=NOW,
                                       user_id=user_id, mode=mode)

            async def for_user(self, session, user_id, exchange_name="bingx", mode=None):  # type: ignore[no-untyped-def]
                return exchange

        redis = FakeRedis()

        def service(**overrides: Any) -> PositionActionService:
            s = settings.model_copy(update=overrides) if overrides else settings
            return PositionActionService(session, s, None, user, redis=redis, factory=Factory(),
                                         market_cache=TTLCache())

        yield SimpleNamespace(session=session, user=user, uid=uid, exchange=exchange,
                              service=service, redis=redis)
        await session.rollback()
        fresh = await UserRepository(session).get_by_id(uid)
        await cleanup_user(session, fresh)
        await session.commit()
    await db.dispose()


async def _card(c, kind=PositionActionKind.MOVE_STOP, params=None, **settings):  # type: ignore[no-untyped-def]
    outcome = await c.service(**settings).open_card(
        kind, params if params is not None else {"breakeven": True}, "XRP-USDT", TradeSide.LONG,
        message_id=55,
    )
    return outcome


async def _rows(c, action_id: int) -> list[ExecutionOrder]:  # type: ignore[no-untyped-def]
    return list(await c.session.scalars(
        select(ExecutionOrder).where(ExecutionOrder.position_action_id == action_id)
        .order_by(ExecutionOrder.id)
    ))


# --- карточка ---------------------------------------------------------------------


async def test_card_row_snapshot(ctx) -> None:  # type: ignore[no-untyped-def]
    outcome = await _card(ctx)
    action = outcome.action
    assert action is not None and action.status is PositionActionStatus.CARD
    assert action.new_level == BE and action.current_stop == D("1.4795")
    assert action.position_qty == D(30) and action.position_id == PID
    assert action.card_message_id == 55 and not outcome.risk_increase
    assert "стоп в безубыток" in outcome.text


async def test_refusal_is_recorded(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.exchange.mark = D("1.5265")   # безубыток ещё не достигнут
    outcome = await _card(ctx)
    assert outcome.refused and outcome.text.startswith("⛔")
    row = await ctx.session.scalar(select(PositionAction).where(PositionAction.user_id == ctx.uid))
    assert row.status is PositionActionStatus.REFUSED and row.error_code == "BREAKEVEN_NOT_REACHED"


async def test_execution_disabled_refuses_before_exchange(ctx) -> None:  # type: ignore[no-untyped-def]
    outcome = await _card(ctx, trading_execution_enabled=False)
    assert outcome.refused and "выключено" in outcome.text
    assert ctx.exchange.calls == []


# --- сухой прогон -------------------------------------------------------------------


async def test_dry_run_writes_rows_and_sends_nothing(ctx) -> None:  # type: ignore[no-untyped-def]
    card = await _card(ctx, exec_dry_run=True)
    result = await ctx.service(exec_dry_run=True).confirm(
        card.action.id, message_id=55, risk_confirmed=False
    )
    assert "Сухой прогон" in result.text and "closePosition" in result.text
    assert "place_conditional" not in ctx.exchange.names()
    await ctx.session.refresh(card.action)
    assert card.action.status is PositionActionStatus.DRY_RUN
    [row] = await _rows(ctx, card.action.id)
    assert row.status is OrderStatus.DRY_RUN and row.trigger_price == BE and row.quantity == D(30)


# --- перенос стопа -------------------------------------------------------------------


def _places(c) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    return [kw for name, kw in c.exchange.calls if name == "place_conditional"]


async def _move(c, **params):  # type: ignore[no-untyped-def]
    card = await _card(c, **params)
    result = await c.service().confirm(card.action.id, message_id=55, risk_confirmed=False)
    await c.session.refresh(card.action)
    return card.action, result.text


async def test_move_stop_four_steps(ctx) -> None:  # type: ignore[no-untyped-def]
    """Мост с quantity → отмена старого → closePosition → отмена моста;
    каждый шаг — read-back по openOrders. Итог на бирже — один стоп на всю
    позицию по новой цене."""
    action, text = await _move(ctx)
    bridge_kw, final_kw = _places(ctx)
    assert bridge_kw["close_position"] is False and final_kw["close_position"] is True
    assert bridge_kw["stop_price"] == final_kw["stop_price"] == BE
    assert bridge_kw["quantity"] == final_kw["quantity"] == D(30)
    assert bridge_kw["client_order_id"] == f"tm{action.id}u{ctx.uid}SB"
    assert final_kw["client_order_id"] == f"tm{action.id}u{ctx.uid}S"
    names = ctx.exchange.names()
    first_cancel = names.index("cancel")
    assert names.index("place_conditional") < first_cancel
    assert ctx.exchange.calls[first_cancel] == ("cancel", OLD_STOP)
    assert names[first_cancel + 1] == "open_orders"   # снятие — повторным openOrders
    assert names[-2:] == ["cancel", "open_orders"]    # мост снят последним
    [stop] = ctx.exchange.orders
    assert stop.close_position and stop.stop_price == BE
    assert action.status is PositionActionStatus.DONE
    assert "Старый стоп и промежуточный сняты" in text
    rows = await _rows(ctx, action.id)
    # Этап 5: строка постановки моста закрывается при подтверждённом снятии
    # (CANCELLED), без отдельной строки-наблюдения; у старого — наблюдение.
    assert [(r.status, r.client_order_id) for r in rows] == [
        (OrderStatus.CANCELLED, f"tm{action.id}u{ctx.uid}SB"),   # мост, снят
        (OrderStatus.CANCELLED, None),                           # старый
        (OrderStatus.SUBMITTED, f"tm{action.id}u{ctx.uid}S"),
    ]
    assert rows[1].exchange_order_id == OLD_STOP


async def test_move_take_four_steps(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.exchange.orders.append(_open_order("2105992400526204928", "1.6", "TAKE_PROFIT_MARKET"))
    action, text = await _move(ctx, kind=PositionActionKind.SET_TAKE, params={"level": "1.65"})
    bridge_kw, final_kw = _places(ctx)
    assert bridge_kw["order_type"] == final_kw["order_type"] == "TAKE_PROFIT_MARKET"
    assert bridge_kw["close_position"] is False and final_kw["close_position"] is True
    assert bridge_kw["client_order_id"] == f"tm{action.id}u{ctx.uid}TB"
    takes = [o for o in ctx.exchange.orders if o.order_type == "TAKE_PROFIT_MARKET"]
    assert [(o.stop_price, o.close_position) for o in takes] == [(D("1.65"), True)]
    assert action.status is PositionActionStatus.DONE and "Тейк 1.6 → 1.65" in text


async def test_set_take_without_old_is_one_order(ctx) -> None:  # type: ignore[no-untyped-def]
    action, text = await _move(ctx, kind=PositionActionKind.SET_TAKE, params={"level": "1.65"})
    [kw] = _places(ctx)
    assert kw["close_position"] is True and kw["client_order_id"] == f"tm{action.id}u{ctx.uid}T"
    assert "cancel" not in ctx.exchange.names()
    assert action.status is PositionActionStatus.DONE and "Тейк 1.65 (на всю позицию)" in text


async def test_step1_bridge_rejected_old_untouched(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.exchange.fail_place_at = {1}
    action, text = await _move(ctx)
    [kw] = _places(ctx)
    assert kw["close_position"] is False
    assert "cancel" not in ctx.exchange.names()
    assert [o.order_id for o in ctx.exchange.orders] == [OLD_STOP]
    assert action.status is PositionActionStatus.FAILED
    assert "не перенесён" in text and "Старый стоп 1.4795 на месте" in text
    [row] = await _rows(ctx, action.id)
    assert row.status is OrderStatus.REJECTED and row.error_code == "109400"


async def test_step1_bridge_not_confirmed_old_untouched(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.exchange.drop_new_at = {1}
    action, text = await _move(ctx)
    assert "cancel" not in ctx.exchange.names()
    assert action.status is PositionActionStatus.FAILED
    assert "не подтверждён" in text and "Старый стоп 1.4795 на месте" in text


async def test_step2_old_not_removed_bridge_removed(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.exchange.cancel_noop_at = {1}
    action, text = await _move(ctx)
    assert len(_places(ctx)) == 1                  # closePosition не ставился
    assert [o.order_id for o in ctx.exchange.orders] == [OLD_STOP]
    assert action.status is PositionActionStatus.FAILED
    assert "старый стоп 1.4795 не снялся" in text and "всё как было" in text
    rows = await _rows(ctx, action.id)
    assert [r.status for r in rows] == [OrderStatus.CANCELLED, OrderStatus.UNKNOWN]


async def test_step2_old_and_bridge_not_removed_two_stops(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.exchange.cancel_noop = True
    action, text = await _move(ctx)
    assert len(ctx.exchange.orders) == 2
    assert action.status is PositionActionStatus.FAILED and "два стопа" in text


async def test_step3_final_rejected_bridge_holds(ctx) -> None:  # type: ignore[no-untyped-def]
    trade = await TradeJournal(TradeRepository(ctx.session)).open_trade(
        user_id=ctx.uid, symbol="XRP-USDT", side=TradeSide.LONG, entry_price=ENTRY,
        quantity=D(30), stop_loss=D("1.4795"), source=TradeSource.IMPORTED,
        account_mode=ExchangeKeyMode.DEMO,
        external_position_id=PID, external_fill_id="f1", fee=D("0.0229"),
    )
    await ctx.session.commit()
    ctx.exchange.fail_place_at = {2}
    action, text = await _move(ctx)
    [left] = ctx.exchange.orders
    assert not left.close_position and left.stop_price == BE     # держит мост
    assert action.status is PositionActionStatus.FAILED
    assert "промежуточным ордером" in text and "не «на всю позицию»" in text
    row = await ctx.session.get(Trade, trade.id)
    await ctx.session.refresh(row)
    assert row.stop_loss == BE        # журнал — по стопу, который стоит на бирже


async def test_step4_bridge_not_removed_warns(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.exchange.cancel_noop_at = {2}
    action, text = await _move(ctx)
    assert sorted(o.close_position for o in ctx.exchange.orders) == [False, True]
    assert action.status is PositionActionStatus.FAILED
    assert "Стоп 1.4795 →" in text and "Промежуточный стоп" in text and "не снялся" in text
    bridge = (await _rows(ctx, action.id))[0]
    assert bridge.status is OrderStatus.SUBMITTED     # не снялся — строка открыта


async def test_bingx_rule_second_close_position_rejected(ctx) -> None:  # type: ignore[no-untyped-def]
    """Сам фейк: closePosition поверх closePosition — 110406, как на демо 02.10
    (старый код переноса падал именно так)."""
    with pytest.raises(ExchangeResponseError) as err:
        await ctx.exchange.place_conditional_order(
            symbol="XRP-USDT", side=SimpleNamespace(value="SELL"), position_side="LONG",
            order_type="STOP_MARKET", stop_price=D("1.5"), quantity=D(30), client_order_id="x",
        )
    assert err.value.code == 110406


async def test_move_stop_updates_linked_journal_trade(ctx) -> None:  # type: ignore[no-untyped-def]
    trade = await TradeJournal(TradeRepository(ctx.session)).open_trade(
        user_id=ctx.uid, symbol="XRP-USDT", side=TradeSide.LONG, entry_price=ENTRY,
        quantity=D(30), stop_loss=D("1.4795"), source=TradeSource.IMPORTED,
        account_mode=ExchangeKeyMode.DEMO,
        external_position_id=PID, external_fill_id="f1", fee=D("0.0229"),
    )
    await ctx.session.commit()
    card = await _card(ctx)
    await ctx.service().confirm(card.action.id, message_id=55, risk_confirmed=False)
    row = await ctx.session.get(Trade, trade.id)
    await ctx.session.refresh(row)
    assert row.initial_stop_loss == D("1.4795") and row.stop_loss == card.action.new_level


# --- рост риска, срок, повтор, лок, расхождение ------------------------------------


async def test_risk_increase_needs_explicit_button(ctx) -> None:  # type: ignore[no-untyped-def]
    card = await _card(ctx, params={"level": "1.40"})
    assert card.risk_increase
    result = await ctx.service().confirm(card.action.id, message_id=55, risk_confirmed=False)
    assert "увеличить риск" in result.text
    assert "place_conditional" not in ctx.exchange.names()
    await ctx.session.refresh(card.action)
    assert card.action.error_code == "RISK_INCREASE_NOT_CONFIRMED"

    again = await _card(ctx, params={"level": "1.40"})
    done = await ctx.service().confirm(again.action.id, message_id=55, risk_confirmed=True)
    assert "Стоп 1.4795 → 1.4" in done.text


async def test_expired_card(ctx) -> None:  # type: ignore[no-untyped-def]
    card = await _card(ctx)
    card.action.created_at = NOW - timedelta(minutes=5)
    await ctx.session.commit()
    result = await ctx.service().confirm(card.action.id, message_id=55, risk_confirmed=False)
    assert "устарела" in result.text
    await ctx.session.refresh(card.action)
    assert card.action.status is PositionActionStatus.EXPIRED


async def test_second_yes_does_nothing(ctx) -> None:  # type: ignore[no-untyped-def]
    card = await _card(ctx)
    await ctx.service().confirm(card.action.id, message_id=55, risk_confirmed=False)
    places = ctx.exchange.names().count("place_conditional")
    again = await ctx.service().confirm(card.action.id, message_id=55, risk_confirmed=False)
    assert "уже обработана" in again.text
    assert ctx.exchange.names().count("place_conditional") == places


async def test_yes_on_other_message_is_stale(ctx) -> None:  # type: ignore[no-untyped-def]
    card = await _card(ctx)
    result = await ctx.service().confirm(card.action.id, message_id=99, risk_confirmed=False)
    assert "не последняя карточка" in result.text
    assert "place_conditional" not in ctx.exchange.names()


async def test_lock_busy(ctx) -> None:  # type: ignore[no-untyped-def]
    card = await _card(ctx)
    key = position_lock_key(ctx.uid, "XRP-USDT", "LONG")
    async with RedisLock(ctx.redis, key, 30):
        result = await ctx.service().confirm(card.action.id, message_id=55, risk_confirmed=False)
    assert not result.final and "уже выполняется" in result.text
    await ctx.session.refresh(card.action)
    assert card.action.status is PositionActionStatus.CARD


async def test_position_changed_after_card(ctx) -> None:  # type: ignore[no-untyped-def]
    card = await _card(ctx)
    ctx.exchange.position = replace(ctx.exchange.position, quantity=D(20))
    result = await ctx.service().confirm(card.action.id, message_id=55, risk_confirmed=False)
    assert "Объём позиции изменился" in result.text
    assert "place_conditional" not in ctx.exchange.names()


async def test_pending_row_blocks_second_order(ctx) -> None:  # type: ignore[no-untyped-def]
    card = await _card(ctx)
    # Перенос начинается с моста — его ключ SB и блокирует повторную отправку.
    ctx.session.add(ExecutionOrder(
        user_id=ctx.uid, client_order_id=f"tm{card.action.id}u{ctx.uid}SB", symbol="XRP-USDT",
        side="SELL", position_side=TradeSide.LONG, order_type="STOP_MARKET",
        role=OrderRole.STOP_LOSS, status=OrderStatus.PENDING,
    ))
    await ctx.session.commit()
    result = await ctx.service().confirm(card.action.id, message_id=55, risk_confirmed=False)
    assert "уже отправлялся" in result.text
    assert "place_conditional" not in ctx.exchange.names()


# --- закрытие ------------------------------------------------------------------------


async def test_partial_close_market_and_journal(ctx) -> None:  # type: ignore[no-untyped-def]
    trade = await TradeJournal(TradeRepository(ctx.session)).open_trade(
        user_id=ctx.uid, symbol="XRP-USDT", side=TradeSide.LONG, entry_price=ENTRY,
        quantity=D(30), stop_loss=D("1.4795"), source=TradeSource.IMPORTED,
        account_mode=ExchangeKeyMode.DEMO,
        external_position_id=PID, external_fill_id="f1", fee=D("0.0229"),
    )
    await ctx.session.commit()
    card = await _card(ctx, PositionActionKind.CLOSE_PARTIAL, {"fraction": "25"})
    result = await ctx.service().confirm(card.action.id, message_id=55, risk_confirmed=False)

    [(_, kw)] = [c for c in ctx.exchange.calls if c[0] == "market"]
    assert kw["quantity"] == D(7) and kw["side"].value == "SELL" and kw["position_side"] == "LONG"
    assert kw["client_order_id"] == f"tm{card.action.id}u{ctx.uid}C"
    assert "cancel" not in ctx.exchange.names()        # стоп не переставляем
    assert "Остаток 23" in result.text
    [row] = await _rows(ctx, card.action.id)
    assert row.status is OrderStatus.FILLED and row.role is OrderRole.CLOSE
    fresh = await ctx.session.scalar(select(Trade).where(Trade.id == trade.id))
    await ctx.session.refresh(fresh, ["fills"])
    assert {f.external_fill_id for f in fresh.fills} == {"f1", row.exchange_order_id}
    assert fresh.is_open


@pytest.mark.parametrize("auto_cancel", [True, False])
async def test_full_close_leftovers(ctx, auto_cancel: bool) -> None:  # type: ignore[no-untyped-def]
    ctx.exchange.auto_cancel_on_close = auto_cancel
    card = await _card(ctx, PositionActionKind.CLOSE_FULL, {})
    result = await ctx.service().confirm(card.action.id, message_id=55, risk_confirmed=False)
    assert ctx.exchange.position is None
    if auto_cancel:
        assert "биржа сняла сама" in result.text and "cancel" not in ctx.exchange.names()
    else:
        assert ("cancel", OLD_STOP) in ctx.exchange.calls
        assert "сняты — проверено по openOrders" in result.text


# --- A.3 (10.10): строки стопа/тейка — CANCELLED с источником по факту ---------------------


async def _placed_stop(c, oid: str, position_id: str) -> ExecutionOrder:  # type: ignore[no-untyped-def]
    """Стоп, поставленный прошлым действием этапа 4 над позицией position_id,
    — строка SUBMITTED с его orderId."""
    action = PositionAction(
        user_id=c.uid, symbol="XRP-USDT", side=TradeSide.LONG, position_id=position_id,
        kind=PositionActionKind.MOVE_STOP, status=PositionActionStatus.DONE,
    )
    c.session.add(action)
    await c.session.flush()
    row = ExecutionOrder(
        user_id=c.uid, position_action_id=action.id, exchange_order_id=oid,
        symbol="XRP-USDT", side=OrderSide.SELL, position_side=TradeSide.LONG,
        order_type=OrderType.STOP_MARKET, role=OrderRole.STOP_LOSS, status=OrderStatus.SUBMITTED,
    )
    c.session.add(row)
    await c.session.commit()
    return row


async def test_move_stop_cancels_old_placed_row_as_user(ctx) -> None:  # type: ignore[no-untyped-def]
    """Ответ владельца (1): снятие старого стопа переносом — USER, и у
    строки, которой он ставился (до A.3 она висела SUBMITTED — строки
    65/67/69). Мост и наблюдение — тоже USER."""
    old = await _placed_stop(ctx, OLD_STOP, PID)
    action, _ = await _move(ctx)
    await ctx.session.refresh(old)
    assert (old.status, old.cancel_source) == (OrderStatus.CANCELLED, CancelSource.USER)
    rows = await _rows(ctx, action.id)
    assert [r.cancel_source for r in rows if r.status is OrderStatus.CANCELLED] == [
        CancelSource.USER, CancelSource.USER,
    ]


@pytest.mark.parametrize(("auto_cancel", "source"), [
    (True, CancelSource.EXCHANGE),     # биржа сняла вместе с позицией
    (False, CancelSource.USER),        # остаток снял бот кнопкой «Закрыть всё» (ответ 1)
])
async def test_full_close_settles_position_rows(  # type: ignore[no-untyped-def]
    ctx, auto_cancel: bool, source: CancelSource
) -> None:
    """Ручная позиция (сделки нет): строки — по positionId её действий;
    строка той же монеты другой позиции не задета."""
    ctx.exchange.auto_cancel_on_close = auto_cancel
    mine = await _placed_stop(ctx, OLD_STOP, PID)
    other = await _placed_stop(ctx, "2105910661355299999", "P-OTHER")
    card = await _card(ctx, PositionActionKind.CLOSE_FULL, {})
    await ctx.service().confirm(card.action.id, message_id=55, risk_confirmed=False)
    await ctx.session.refresh(mine)
    await ctx.session.refresh(other)
    assert (mine.status, mine.cancel_source) == (OrderStatus.CANCELLED, source)
    assert other.status is OrderStatus.SUBMITTED


async def test_full_close_standing_order_row_untouched(ctx, caplog) -> None:  # type: ignore[no-untyped-def]
    """Остаток не снялся и стоит в openOrders — строку не трогаем, WARNING."""
    ctx.exchange.auto_cancel_on_close = False
    ctx.exchange.cancel_noop = True
    mine = await _placed_stop(ctx, OLD_STOP, PID)
    card = await _card(ctx, PositionActionKind.CLOSE_FULL, {})
    with caplog.at_level("WARNING"):
        await ctx.service().confirm(card.action.id, message_id=55, risk_confirmed=False)
    await ctx.session.refresh(mine)
    assert mine.status is OrderStatus.SUBMITTED and mine.cancel_source is None
    assert any(r.getMessage() == "Условный ордер ещё стоит после закрытия позиции"
               for r in caplog.records)
