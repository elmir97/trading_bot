"""Действия с позицией (этап 4, app/execution/position_action_service.py)
против настоящей БД и фейковой биржи с состоянием: карточка, сухой прогон,
перенос стопа/тейка в четыре шага (мост с quantity → отмена старого →
closePosition → отмена моста; фейк, как BingX, не принимает второй
closePosition-ордер того же типа — 110406/110407) и сбой на каждом шаге,
закрытие (маркет → get_order_fill → журнал), рост риска, срок карточки,
повторное «Да», лок, расхождение позиции, идемпотентность по client_order_id.
"""

from __future__ import annotations

import logging
import os
from dataclasses import replace
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
from app.exchanges.base import (
    Balance,
    CancelResult,
    ExchangeResponseError,
    ExchangeUnavailableError,
    OpenOrder,
    OrderFill,
    OrderResult,
    Position,
    ReadbackIncomplete,
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
        # Комиссия исполнения закрытия; по умолчанию — прежнее значение (К4: «<0.01» — меньше).
        self.close_fee = D("0.0234")
        # Деплой 4: ошибки чтения по cid — по одной на вызов, пока не кончатся.
        self.fill_errors: list[Exception] = []
        # Деплой 4, 4.2: статус и поля ответа на POST маркета.
        self.post_status = "FILLED"
        self.post_raw: dict[str, object] = {}

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    async def get_positions(self, *, max_attempts=None) -> list[Position]:  # type: ignore[no-untyped-def]
        self.calls.append(("positions", None))
        return [self.position] if self.position else []

    async def get_open_orders(self, symbol=None, *, max_attempts=None) -> list[OpenOrder]:  # type: ignore[no-untyped-def]
        self.calls.append(("open_orders", symbol))
        return list(self.orders)

    async def get_mark_price(self, symbol: str) -> Decimal:
        return self.mark

    async def get_symbols(self, *, max_attempts=None) -> list[SymbolInfo]:  # type: ignore[no-untyped-def]
        return [SymbolInfo("XRP-USDT", 4, 0, D(2), D(2))]

    async def get_balance(self, *, max_attempts=None) -> Balance:  # type: ignore[no-untyped-def]
        return Balance("USDT", D(9000), D(100), D(0), D(10000))

    async def get_position_mode(self, *, max_attempts=None) -> bool:  # type: ignore[no-untyped-def]
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
            self.close_fee, {}, NOW,
        )
        return OrderResult(oid, kw["client_order_id"], kw["symbol"], kw["side"].value,
                           kw["position_side"], "MARKET", self.post_status, self.post_raw)

    async def get_order_fill(self, symbol, client_order_id, *, max_attempts=None) -> OrderFill:  # type: ignore[no-untyped-def]
        self.calls.append(("fill", client_order_id))
        if self.fill_errors:
            raise self.fill_errors.pop(0)
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
                              settings=settings,
                              service=service, redis=redis, db=db)
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
    assert action.status is PositionActionStatus.DONE and "Тейк 1.6000 → 1.6500" in text


async def test_set_take_without_old_is_one_order(ctx) -> None:  # type: ignore[no-untyped-def]
    action, text = await _move(ctx, kind=PositionActionKind.SET_TAKE, params={"level": "1.65"})
    [kw] = _places(ctx)
    assert kw["close_position"] is True and kw["client_order_id"] == f"tm{action.id}u{ctx.uid}T"
    assert "cancel" not in ctx.exchange.names()
    assert action.status is PositionActionStatus.DONE and "Тейк 1.6500 (на всю позицию)" in text


async def test_set_take_rounded_closer_on_exchange(ctx) -> None:  # type: ignore[no-untyped-def]
    """A.4: тейк этапа 4 — к шагу цены ближе к цене (LONG вниз), на бирже и в базе."""
    action, text = await _move(ctx, kind=PositionActionKind.SET_TAKE, params={"level": "1.65009"})
    [kw] = _places(ctx)
    assert kw["stop_price"] == D("1.6500")
    assert action.new_level == D("1.6500") and action.params == {"level": "1.6500"}
    [row] = await _rows(ctx, action.id)
    assert row.trigger_price == D("1.6500")
    assert action.status is PositionActionStatus.DONE and "Тейк 1.6500 (на всю позицию)" in text


@pytest.mark.parametrize(
    ("kind", "level", "posted"),
    [
        # объём фиксирован — ближе к цене: стоп LONG вверх, тейк LONG вниз
        (PositionActionKind.MOVE_STOP, "1.50001", D("1.5001")),
        (PositionActionKind.SET_TAKE, "1.65009", D("1.6500")),
    ],
)
async def test_off_step_level_rounded_before_post(  # type: ignore[no-untyped-def]
    ctx, monkeypatch, caplog, kind, level, posted,
) -> None:
    """Защита в самом POST: план пришёл не на шаге (например, сменилась точность
    контракта между карточкой и «Да») — округлить, записать, WARNING."""
    import app.execution.position_action_service as svc

    real = svc.plan_action

    def unrounded(k, params, inputs):  # type: ignore[no-untyped-def]
        plan = real(k, params, inputs)
        return replace(plan, new_level=D(level), params={"level": level})

    monkeypatch.setattr(svc, "plan_action", unrounded)
    with caplog.at_level(logging.INFO):
        action, _ = await _move(ctx, kind=kind, params={"level": level})
    assert action.status is PositionActionStatus.DONE
    assert {kw["stop_price"] for kw in _places(ctx)} == {posted}
    assert action.new_level == posted and action.params == {"level": str(posted)}
    assert {r.trigger_price for r in await _rows(ctx, action.id) if r.client_order_id} == {posted}
    warned = [r for r in caplog.records
              if r.getMessage() == "Уровень действия не на шаге цены — округлён перед POST"]
    assert [(D(r.before), D(r.after)) for r in warned] == [(D(level), posted)]  # type: ignore[attr-defined]


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
        account_mode=ExchangeKeyMode.DEMO,
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
        account_mode=ExchangeKeyMode.DEMO,
        user_id=c.uid, symbol="XRP-USDT", side=TradeSide.LONG, position_id=position_id,
        kind=PositionActionKind.MOVE_STOP, status=PositionActionStatus.DONE,
    )
    c.session.add(action)
    await c.session.flush()
    row = ExecutionOrder(
        account_mode=ExchangeKeyMode.DEMO,
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


async def test_full_close_exchange_only_for_orders_standing_before_post(ctx) -> None:  # type: ignore[no-untyped-def]
    """п.1б (24.10): EXCHANGE — только ордерам, стоявшим в openOrders перед
    POST под локом. Строка, чьего ордера там уже не было (снят раньше), —
    CANCELLED без источника: кто снял, не доказано."""
    mine = await _placed_stop(ctx, OLD_STOP, PID)
    stray = await _placed_stop(ctx, "2105910661355299998", PID)    # в openOrders его нет
    card = await _card(ctx, PositionActionKind.CLOSE_FULL, {})
    await ctx.service().confirm(card.action.id, message_id=55, risk_confirmed=False)
    await ctx.session.refresh(mine)
    await ctx.session.refresh(stray)
    assert (mine.status, mine.cancel_source) == (OrderStatus.CANCELLED, CancelSource.EXCHANGE)
    assert (stray.status, stray.cancel_source) == (OrderStatus.CANCELLED, None)


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


# --- деплой 4, 4.1: причина неудачного read-back закрытия в логе ---------------------------

SECRET = "apiKey-эхо-не-печатать"


async def test_close_readback_failures_logged_with_reason(ctx, caplog) -> None:  # type: ignore[no-untyped-def]
    """11.10, #37: «не подтверждено» без единой строки причины. Каждая неудачная
    попытка — INFO с типом/кодом/полем, все неудачные — WARNING; ответ биржи
    целиком в лог не попадает."""
    ctx.exchange.fill_errors = [
        ReadbackIncomplete("avgPrice", payload={"status": "FILLED", "apiKey": SECRET}),
        ExchangeResponseError("BingX: order not exist (код 109421)", code=109421),
        ExchangeUnavailableError("BingX не ответил вовремя"),
    ]
    card = await _card(ctx, PositionActionKind.CLOSE_FULL, {})
    with caplog.at_level("INFO"):
        await ctx.service().confirm(card.action.id, message_id=55, risk_confirmed=False)
    tries = [r for r in caplog.records if r.getMessage() == "Закрытие не подтверждено по cid"]
    got = [(r.attempt, r.error, r.code, getattr(r, "field", None), getattr(r, "status", None),  # type: ignore[attr-defined]
            r.cid) for r in tries]  # type: ignore[attr-defined]
    cid = f"tm{card.action.id}u{ctx.uid}C"
    assert got == [
        (1, "ReadbackIncomplete", None, "avgPrice", "FILLED", cid),
        (2, "ExchangeResponseError", 109421, None, None, cid),
        (3, "ExchangeUnavailableError", None, None, None, cid),
    ]
    final = [r for r in caplog.records if r.getMessage() == "Закрытие не подтверждено за 3 попыток"]
    assert len(final) == 1 and final[0].levelno == 30
    assert SECRET not in str([r.__dict__ for r in caplog.records])


async def test_close_readback_second_try_logs_one_attempt(ctx, caplog) -> None:  # type: ignore[no-untyped-def]
    ctx.exchange.fill_errors = [ReadbackIncomplete("commission", payload={"status": "FILLED"})]
    card = await _card(ctx, PositionActionKind.CLOSE_FULL, {})
    with caplog.at_level("INFO"):
        await ctx.service().confirm(card.action.id, message_id=55, risk_confirmed=False)
    await ctx.session.refresh(card.action)
    assert card.action.status is PositionActionStatus.DONE
    tries = [r for r in caplog.records if r.getMessage() == "Закрытие не подтверждено по cid"]
    assert [(r.attempt, r.field) for r in tries] == [(1, "commission")]  # type: ignore[attr-defined]
    assert not any(r.getMessage().startswith("Закрытие не подтверждено за") for r in caplog.records)


# --- деплой 4, 4.2: POST маркета вернул FILLED — закрытие исполнено ------------------------

def _read_down(c, n: int = 3) -> None:  # type: ignore[no-untyped-def]
    c.exchange.fill_errors = [ExchangeUnavailableError("BingX не ответил вовремя")] * n


async def _imported_trade(c) -> Trade:  # type: ignore[no-untyped-def]
    trade = await TradeJournal(TradeRepository(c.session)).open_trade(
        user_id=c.uid, symbol="XRP-USDT", side=TradeSide.LONG, entry_price=ENTRY,
        quantity=D(30), stop_loss=D("1.4795"), source=TradeSource.IMPORTED,
        account_mode=ExchangeKeyMode.DEMO,
        external_position_id=PID, external_fill_id="f1", fee=D("0.0229"),
    )
    await c.session.commit()
    return trade


async def test_full_close_post_filled_read_down_is_done(ctx, caplog) -> None:  # type: ignore[no-untyped-def]
    """#37 (11.10): позиция закрылась, чтение по cid не подтвердило. Теперь —
    DONE, строка FILLED с price NULL («не дочитано»), выход в журнал не
    пишется (его допишет reconciler), остатки на бирже разобраны как обычно.
    В лог — имена полей ответа POST и updateTime, apiKey — только именем."""
    trade = await _imported_trade(ctx)
    ctx.exchange.post_raw = {"status": "FILLED", "updateTime": 1760212871343, "apiKey": SECRET}
    _read_down(ctx)
    card = await _card(ctx, PositionActionKind.CLOSE_FULL, {})
    with caplog.at_level("INFO"):
        result = await ctx.service().confirm(card.action.id, message_id=55, risk_confirmed=False)
    assert result.text.startswith(
        "✅ Закрыто 30 — цена выхода биржа ещё не отдала, итог в журнале следом."
    )
    assert "Стоп и тейк биржа сняла сама" in result.text
    await ctx.session.refresh(card.action)
    assert card.action.status is PositionActionStatus.DONE and card.action.error_code is None
    [row] = [r for r in await _rows(ctx, card.action.id) if r.role is OrderRole.CLOSE]
    assert row.status is OrderStatus.FILLED and row.price is None and row.quantity == D(30)
    assert row.exchange_order_id
    fresh = await ctx.session.scalar(select(Trade).where(Trade.id == trade.id))
    assert fresh.is_open                      # выход — за reconciler
    [rec] = [r for r in caplog.records if r.getMessage() == "Закрытие исполнено по ответу POST"]
    assert rec.post_fields == ["apiKey", "status", "updateTime"]  # type: ignore[attr-defined]
    assert rec.post_updateTime == "1760212871343"  # type: ignore[attr-defined]
    assert SECRET not in str([r.__dict__ for r in caplog.records])


async def test_partial_close_post_filled_read_down_rest_from_sent(ctx) -> None:  # type: ignore[no-untyped-def]
    await _imported_trade(ctx)
    _read_down(ctx)
    card = await _card(ctx, PositionActionKind.CLOSE_PARTIAL, {"fraction": "25"})
    result = await ctx.service().confirm(card.action.id, message_id=55, risk_confirmed=False)
    assert result.text.startswith("✅ Закрыто 7 — цена выхода биржа ещё не отдала")
    assert "Остаток 23" in result.text
    await ctx.session.refresh(card.action)
    assert card.action.status is PositionActionStatus.DONE


async def test_close_post_not_filled_read_down_stays_unconfirmed(ctx) -> None:  # type: ignore[no-untyped-def]
    """POST без FILLED — прежний путь: «⚠️ не подтверждено», строка UNKNOWN."""
    ctx.exchange.post_status = "NEW"
    _read_down(ctx)
    card = await _card(ctx, PositionActionKind.CLOSE_FULL, {})
    result = await ctx.service().confirm(card.action.id, message_id=55, risk_confirmed=False)
    assert result.text.startswith("⚠️ Исполнение не подтверждено")
    await ctx.session.refresh(card.action)
    assert card.action.status is PositionActionStatus.FAILED
    [row] = [r for r in await _rows(ctx, card.action.id) if r.role is OrderRole.CLOSE]
    assert row.status is OrderStatus.UNKNOWN


async def test_close_fee_below_cent_text(ctx) -> None:  # type: ignore[no-untyped-def]
    """К4 (28.10.2026): комиссия исполнения меньше цента — «<0.01», не «0.00» (текст чата —
    HTML: «&lt;0.01»)."""
    ctx.exchange.close_fee = D("0.0042")
    card = await _card(ctx, PositionActionKind.CLOSE_FULL, {})
    result = await ctx.service().confirm(card.action.id, message_id=55, risk_confirmed=False)
    assert "✅ Закрыто 30 по 1.5600 · комиссия &lt;0.01 USDT." in result.text


async def test_close_post_filled_read_ok_journals_exit(ctx) -> None:  # type: ignore[no-untyped-def]
    """Норма: POST FILLED и чтение прошло — цена и выход в журнале сразу."""
    trade = await _imported_trade(ctx)
    card = await _card(ctx, PositionActionKind.CLOSE_FULL, {})
    result = await ctx.service().confirm(card.action.id, message_id=55, risk_confirmed=False)
    assert result.text.startswith("✅ Закрыто 30 по 1.56")
    [row] = [r for r in await _rows(ctx, card.action.id) if r.role is OrderRole.CLOSE]
    assert row.status is OrderStatus.FILLED and row.price == D("1.5600")
    fresh = await ctx.session.scalar(select(Trade).where(Trade.id == trade.id))
    assert not fresh.is_open


# --- п.2 (24.10): срок карточек действий ------------------------------------------------


def _card_ttl(c) -> timedelta:  # type: ignore[no-untyped-def]
    s = c.service()._settings
    return timedelta(seconds=s.exec_confirm_ttl_seconds + s.confirm_lock_ttl_seconds)


async def test_stale_action_cards_expire_only_card(ctx) -> None:  # type: ignore[no-untyped-def]
    """CARD старше exec_confirm_ttl + TTL лока → EXPIRED, CARD_EXPIRED,
    decided_at. Свежая CARD и старое SUBMITTED не тронуты."""
    from app.execution.action_recovery import expire_stale_action_cards

    old, fresh, sent = [(await _card(ctx)).action for _ in range(3)]
    ttl = _card_ttl(ctx)
    old.created_at = datetime.now(UTC) - ttl - timedelta(seconds=5)
    fresh.created_at = datetime.now(UTC) - ttl + timedelta(seconds=30)
    sent.created_at = old.created_at
    sent.status = PositionActionStatus.SUBMITTED
    await ctx.session.commit()

    assert await expire_stale_action_cards(ctx.db, ctx.service()._settings) >= 1

    for a in (old, fresh, sent):
        await ctx.session.refresh(a)
    assert (old.status, old.error_code) == (PositionActionStatus.EXPIRED, "CARD_EXPIRED")
    assert old.decided_at is not None
    assert fresh.status is PositionActionStatus.CARD and fresh.decided_at is None
    assert sent.status is PositionActionStatus.SUBMITTED


async def test_yes_on_expired_card_says_stale_sends_nothing(ctx) -> None:  # type: ignore[no-untyped-def]
    card = await _card(ctx, PositionActionKind.CLOSE_FULL, {})
    card.action.status = PositionActionStatus.EXPIRED
    card.action.error_code = "CARD_EXPIRED"
    await ctx.session.commit()
    result = await ctx.service().confirm(card.action.id, message_id=55, risk_confirmed=False)
    assert result.text == "Карточка устарела — открой действие заново."
    assert not {"market", "place_conditional", "cancel"} & set(ctx.exchange.names())


@pytest.mark.parametrize("kind,params", [
    (PositionActionKind.CLOSE_FULL, {}),
    (PositionActionKind.MOVE_STOP, {"breakeven": True}),
])
async def test_card_expired_during_yes_nothing_sent(ctx, kind, params) -> None:  # type: ignore[no-untyped-def]
    """Гонка: «Да» прошёл проверки, а воркер в этот момент закрыл карточку
    сроком. Переход CARD → SUBMITTED условный: rowcount 0 — без отправки,
    действие остаётся EXPIRED (не FAILED), строки PENDING нет."""
    card = await _card(ctx, kind, params)
    action_id = card.action.id
    real = ctx.exchange.get_positions

    async def expire_then_read(**kw):  # type: ignore[no-untyped-def]
        async with ctx.db.session() as other:
            await other.execute(
                update(PositionAction).where(PositionAction.id == action_id)
                .values(status=PositionActionStatus.EXPIRED, error_code="CARD_EXPIRED")
            )
            await other.commit()
        return await real(**kw)

    ctx.exchange.get_positions = expire_then_read
    result = await ctx.service().confirm(action_id, message_id=55, risk_confirmed=False)
    assert result.text == "Карточка устарела — открой действие заново."
    assert not {"market", "place_conditional", "cancel"} & set(ctx.exchange.names())
    assert await _rows(ctx, action_id) == []
    action = await ctx.session.get(PositionAction, action_id, populate_existing=True)
    assert action is not None and action.status is PositionActionStatus.EXPIRED


# --- 5.0: превью Mini App, счёт карточки, текст объёма (25.10.2026) ----------------

_WRITES = {"market", "place_conditional", "cancel"}


async def _action_rows(c) -> list[PositionAction]:  # type: ignore[no-untyped-def]
    return list(await c.session.scalars(
        select(PositionAction).where(PositionAction.user_id == c.uid)
    ))


@pytest.mark.parametrize("kind,params", [
    (PositionActionKind.MOVE_STOP, {"breakeven": True}),
    (PositionActionKind.MOVE_STOP, {"level_percent": "1"}),
    (PositionActionKind.SET_TAKE, {"level": "1.7"}),
    (PositionActionKind.CLOSE_PARTIAL, {"fraction": "50"}),
    (PositionActionKind.CLOSE_FULL, {}),
])
async def test_preview_writes_nothing(ctx, kind, params) -> None:  # type: ignore[no-untyped-def]
    """Превью — тот же расчёт, что у карточки, но ни строки действия, ни ордеров."""
    outcome = await ctx.service().preview(kind, params, "XRP-USDT", TradeSide.LONG)
    assert outcome.action is None and outcome.refusal is None and outcome.view is not None
    assert await _action_rows(ctx) == []
    card = await _card(ctx, kind, params)
    assert outcome.text == card.text and outcome.view == card.view
    assert [a.id for a in await _action_rows(ctx)] == [card.action.id]   # только карточка
    assert not _WRITES & set(ctx.exchange.names())


async def test_preview_refusal_writes_nothing(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.exchange.mark = D("1.5265")   # безубыток ещё не достигнут
    outcome = await ctx.service().preview(
        PositionActionKind.MOVE_STOP, {"breakeven": True}, "XRP-USDT", TradeSide.LONG
    )
    assert outcome.view is None and outcome.refusal is not None
    assert outcome.refusal.code.value == "BREAKEVEN_NOT_REACHED"
    assert outcome.text.startswith("⛔") and await _action_rows(ctx) == []


async def test_preview_disabled_execution_no_exchange(ctx) -> None:  # type: ignore[no-untyped-def]
    outcome = await ctx.service(trading_execution_enabled=False).preview(
        PositionActionKind.CLOSE_FULL, {}, "XRP-USDT", TradeSide.LONG
    )
    assert outcome.refusal is not None and ctx.exchange.calls == []
    assert await _action_rows(ctx) == []


async def test_card_refusal_carries_reason(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.exchange.mark = D("1.5265")
    outcome = await _card(ctx)
    assert outcome.refused and outcome.view is None
    assert outcome.refusal is not None and outcome.refusal.code.value == "BREAKEVEN_NOT_REACHED"


async def test_level_percent_card_stores_price_and_yes_uses_it(ctx) -> None:  # type: ignore[no-untyped-def]
    """Чип «1%»: стоп на 1% ниже mark (LONG), в строку — цена; mark ушёл до
    «Да» — уходит та же цена, что на карточке (не 1% от нового mark)."""
    card = await _card(ctx, params={"level_percent": "1"})
    expected = D("1.5444")   # 1.56 × 0.99 — на шаге
    assert card.action.new_level == expected
    assert card.action.params == {"level": "1.5444", "level_percent": "1"}
    ctx.exchange.mark = D("1.58")
    result = await ctx.service().confirm(card.action.id, message_id=55, risk_confirmed=False)
    places = _places(ctx)
    assert places and all(kw["stop_price"] == expected for kw in places), result.text
    await ctx.session.refresh(card.action)
    assert card.action.status is PositionActionStatus.DONE


async def test_volume_drift_text_has_no_exponent(ctx) -> None:  # type: ignore[no-untyped-def]
    """normalize() у 30 давал «3E+1» (25.10)."""
    card = await _card(ctx)
    ctx.exchange.position = replace(ctx.exchange.position, quantity=D(23))
    result = await ctx.service().confirm(card.action.id, message_id=55, risk_confirmed=False)
    assert "Объём позиции изменился: 30 → 23 — открой заново." in result.text
    assert not _WRITES & set(ctx.exchange.names())


async def test_other_account_card_not_found_by_service(ctx) -> None:  # type: ignore[no-untyped-def]
    """5.0: карточка DEMO, конфиг сменили на live — «Да»/«Нет» её не находят,
    на биржу ничего; строка остаётся CARD (истечёт воркером)."""
    card = await _card(ctx)
    calls_before = len(ctx.exchange.calls)
    live = ctx.service(bingx_trading_mode="live")
    assert await live.load(card.action.id) is None
    result = await live.confirm(card.action.id, message_id=55, risk_confirmed=False)
    assert result.text == "Карточка уже обработана — открой действие заново."
    assert await live.decline(card.action.id) == "Карточка уже обработана."
    assert len(ctx.exchange.calls) == calls_before
    await ctx.session.refresh(card.action)
    assert card.action.status is PositionActionStatus.CARD
    assert await ctx.service().load(card.action.id) is not None


async def test_bot_yes_on_other_account_card(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Поведение бота меняется (5.0): «Да» в чате на карточке другого счёта —
    «Карточка уже обработана…», 0 запросов к бирже, карточка не исполнена."""
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.types import CallbackQuery, Message

    from app.bot.handlers import position_actions as handlers

    card = await _card(ctx)
    calls_before = len(ctx.exchange.calls)
    monkeypatch.setattr(handlers, "record_callback", AsyncMock())
    edited = AsyncMock()
    monkeypatch.setattr(handlers.outbox, "edit", edited)
    callback = MagicMock(spec=CallbackQuery)
    callback.data = f"pm:y:{card.action.id}"
    callback.id = "cb-5-0"
    message = MagicMock(spec=Message)
    message.message_id = 55
    message.chat = SimpleNamespace(id=1)
    callback.message = message
    callback.answer = AsyncMock()
    live = ctx.settings.model_copy(update={"bingx_trading_mode": "live"})
    await handlers.decide(callback, ctx.session, ctx.user, live, None, MagicMock(), ctx.redis)  # type: ignore[arg-type]
    assert edited.await_args.args[3] == "Карточка уже обработана — открой действие заново."
    assert len(ctx.exchange.calls) == calls_before
    await ctx.session.refresh(card.action)
    assert card.action.status is PositionActionStatus.CARD
