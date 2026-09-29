"""Решения reconciler (шаг 15.6) на живых ответах демо 27.09.

SOL #4 закрыта на бирже стопом (дочерний ордер 2104219661398712320,
triggerOrderId = стоп бота 2104213344721920001), LINK #3 открыта с живыми
SL/TP. Остальные случаи — производные от тех же живых ордеров.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.exchanges.base import OpenOrder, OrderFill, Position
from app.exchanges.bingx import BingXClient, _parse_history_order
from app.execution.reconciler import (
    BotTradeSnapshot,
    UnresolvedEntry,
    decide_trade,
    entry_is_due,
    exchange_position,
    needs_history,
    orphan_positions,
    resolve_entry,
    stop_missing,
)
from app.trading.enums import ReconciliationKind, TradeSide
from app.trading.exit_reasons import EXIT_OUTSIDE_BOT, EXIT_STOP_LOSS, EXIT_TAKE_PROFIT
from tests.bingx_fixtures import LINK_MANUAL_STOP, live_items

D = Decimal

# Тексты app.trading.exit_reasons 29.09 — строками, не импортом: на коде до
# правки файл обязан импортироваться, а новые тесты — падать на поведении.
MANUAL_STOP_REASON = "Стоп, изменённый вручную — закрыто вне бота"
MANUAL_TAKE_REASON = "Тейк, изменённый вручную — закрыто вне бота"

SOL_ENTRY = "2104213344135159808"
SOL_STOP = "2104213344721920001"
SOL_TAKE = "2104213344721920000"
SOL_STOP_CHILD = "2104219661398712320"


def _sol_trade(**overrides: object) -> BotTradeSnapshot:
    fields: dict[str, object] = {
        "trade_id": 4, "symbol": "SOL-USDT", "side": TradeSide.LONG,
        "open_quantity": D("1362.07"),
        "opened_at": datetime(2026, 9, 27, 14, 15, 30, 300000, tzinfo=UTC),
        "stop_order_id": SOL_STOP, "take_order_id": SOL_TAKE,
        "recorded_order_ids": frozenset({SOL_ENTRY}),
    }
    fields.update(overrides)
    return BotTradeSnapshot(**fields)  # type: ignore[arg-type]


def _sol_orders():  # type: ignore[no-untyped-def]
    return [_parse_history_order(item) for item in live_items("allOrders SOL")]


def _live_positions() -> list[Position]:
    import asyncio

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "msg": "", "data": live_items("positions all")})

    async def load() -> list[Position]:
        client = BingXClient(
            api_key="k", api_secret="s", client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler), base_url="https://test"
            ),
        )
        try:
            return await client.get_positions()
        finally:
            await client.close()

    return asyncio.run(load())


# --- SOL #4: закрыта стопом ---------------------------------------------------


def test_sol_closed_by_stop_is_recorded_from_fact() -> None:
    positions = _live_positions()
    trade = _sol_trade()
    position = exchange_position(positions, "SOL-USDT", TradeSide.LONG)
    assert position is None and needs_history(trade, position)

    decision = decide_trade(trade, position, _sol_orders())

    assert decision.discrepancy is None
    assert decision.closes_fully is True
    [exit_fill] = decision.exits
    assert exit_fill.order_id == SOL_STOP_CHILD
    assert (exit_fill.price, exit_fill.quantity, exit_fill.fee) == (
        D("121.611"), D("1362.07"), D("82.821383"),
    )
    assert exit_fill.executed_at == datetime(2026, 9, 27, 14, 40, 36, tzinfo=UTC)
    assert (exit_fill.reason, exit_fill.kind) == (
        EXIT_STOP_LOSS, ReconciliationKind.CLOSED_STOP_LOSS,
    )


def test_already_recorded_exit_is_not_recorded_again() -> None:
    """Повторный проход по уже закрытой (в журнале объём 0) сделке ничего не
    пишет: выход известен, позиции нет, закрывать нечего."""
    trade = _sol_trade(
        open_quantity=D("0"), recorded_order_ids=frozenset({SOL_ENTRY, SOL_STOP_CHILD}),
    )
    decision = decide_trade(trade, None, _sol_orders())
    assert decision.exits == [] and decision.discrepancy is None


# --- LINK #3: всё сходится ------------------------------------------------------


def test_link_open_with_live_stop_is_consistent() -> None:
    positions = _live_positions()
    trade = BotTradeSnapshot(
        trade_id=3, symbol="LINK-USDT", side=TradeSide.LONG, open_quantity=D("2037.8"),
        opened_at=datetime(2026, 9, 27, 8, 15, 32, 828000, tzinfo=UTC),
        stop_order_id="2104122758140616705", take_order_id="2104122758140616704",
    )
    position = exchange_position(positions, "LINK-USDT", TradeSide.LONG)
    assert position is not None and not needs_history(trade, position)
    assert decide_trade(trade, position, []).exits == []
    open_orders = [
        BingXClient._parse_open_order(item) for item in live_items("openOrders LINK")
    ]
    assert stop_missing(trade, position, open_orders) is None
    assert orphan_positions(positions, {("LINK-USDT", TradeSide.LONG)}, set()) == []


# --- Производные случаи ---------------------------------------------------------


def _stop_child():  # type: ignore[no-untyped-def]
    return next(o for o in _sol_orders() if o.order_id == SOL_STOP_CHILD)


def test_take_profit_classified_by_trigger() -> None:
    child = replace(_stop_child(), order_type="TAKE_PROFIT_MARKET", trigger_order_id=SOL_TAKE)
    decision = decide_trade(_sol_trade(), None, [child])
    assert [(e.reason, e.kind) for e in decision.exits] == [
        (EXIT_TAKE_PROFIT, ReconciliationKind.CLOSED_TAKE_PROFIT),
    ]


def test_manual_market_close_is_outside_bot() -> None:
    manual = replace(_stop_child(), order_type="MARKET", trigger_order_id=None, order_id="M1")
    decision = decide_trade(_sol_trade(), None, [manual])
    assert [(e.reason, e.kind) for e in decision.exits] == [
        (EXIT_OUTSIDE_BOT, ReconciliationKind.CLOSED_OUTSIDE_BOT),
    ]
    assert decision.closes_fully is True


def test_partial_close_keeps_trade_open() -> None:
    part = replace(
        _stop_child(), order_type="MARKET", trigger_order_id=None, order_id="P1",
        executed_qty=D("362.07"),
    )
    remaining = Position(
        symbol="SOL-USDT", side=TradeSide.LONG, quantity=D("1000"), entry_price=D("123.021"),
        mark_price=D("122"), leverage=10, unrealized_pnl=D("0"), margin=D("0"),
    )
    decision = decide_trade(_sol_trade(), remaining, [part])
    assert decision.closes_fully is False
    assert [(e.quantity, e.kind) for e in decision.exits] == [
        (D("362.07"), ReconciliationKind.PARTIAL_CLOSE),
    ]


def test_quantity_not_matching_is_discrepancy_not_write() -> None:
    short = replace(_stop_child(), executed_qty=D("1000"))
    decision = decide_trade(_sol_trade(), None, [short])
    assert decision.exits == []
    assert decision.discrepancy is not None
    assert decision.discrepancy.kind is ReconciliationKind.QUANTITY_MISMATCH


def test_position_grown_outside_bot_is_discrepancy() -> None:
    bigger = Position(
        symbol="SOL-USDT", side=TradeSide.LONG, quantity=D("2000"), entry_price=D("123"),
        mark_price=D("122"), leverage=10, unrealized_pnl=D("0"), margin=D("0"),
    )
    decision = decide_trade(_sol_trade(), bigger, [])
    assert decision.discrepancy is not None
    assert decision.discrepancy.kind is ReconciliationKind.QUANTITY_MISMATCH


def test_unrecognised_closing_order_is_ambiguous() -> None:
    odd = replace(_stop_child(), order_type="LIQUIDATION", trigger_order_id=None, order_id="L1")
    decision = decide_trade(_sol_trade(), None, [odd])
    assert decision.exits == []
    assert decision.discrepancy is not None
    assert decision.discrepancy.kind is ReconciliationKind.AMBIGUOUS


def test_orders_before_entry_are_ignored() -> None:
    old = replace(
        _stop_child(), order_id="OLD", trigger_order_id=None, order_type="MARKET",
        updated_at=datetime(2026, 9, 26, tzinfo=UTC),
    )
    decision = decide_trade(_sol_trade(), None, [old, _stop_child()])
    assert [e.order_id for e in decision.exits] == [SOL_STOP_CHILD]


def test_position_without_stop_is_stop_missing() -> None:
    [link] = _live_positions()
    trade = BotTradeSnapshot(
        trade_id=3, symbol="LINK-USDT", side=TradeSide.LONG, open_quantity=D("2037.8"),
        opened_at=datetime(2026, 9, 27, 8, 15, tzinfo=UTC),
        stop_order_id=None, take_order_id=None,
    )
    found = stop_missing(trade, link, [])
    assert found is not None
    assert (found.kind, found.dedup_key) == (ReconciliationKind.STOP_MISSING, "stop_missing:3")


def test_orphan_position_reported_unless_in_flight() -> None:
    positions = _live_positions()
    [orphan] = orphan_positions(positions, set(), set())
    assert orphan.kind is ReconciliationKind.ORPHAN_POSITION
    assert orphan.dedup_key == "orphan:LINK-USDT:LONG:2104122757805514754"
    assert orphan_positions(positions, set(), {"LINK-USDT"}) == []


# --- UNKNOWN / PENDING ----------------------------------------------------------


def _unresolved(minutes_ago: int) -> UnresolvedEntry:
    return UnresolvedEntry(
        execution_order_id=40, client_order_id="tj215u1E", symbol="SOL-USDT",
        side=TradeSide.LONG, status="UNKNOWN",
        created_at=datetime(2026, 9, 27, 14, 15, tzinfo=UTC) - timedelta(minutes=minutes_ago),
        trade_id=4,
    )


def test_entry_due_only_after_window() -> None:
    now = datetime(2026, 9, 27, 14, 15, tzinfo=UTC)
    assert entry_is_due(_unresolved(9), now) is False
    assert entry_is_due(_unresolved(10), now) is True


def test_found_filled_entry_is_confirmed() -> None:
    [raw] = live_items("order #40 SOL-USDT ENTRY")
    fill: OrderFill = BingXClient._parse_order_fill(raw)
    resolution = resolve_entry(_unresolved(11), fill, not_found=False, position=None)
    assert resolution.confirmed is fill and resolution.not_placed is False


def test_not_found_without_position_is_not_placed() -> None:
    resolution = resolve_entry(_unresolved(11), None, not_found=True, position=None)
    assert resolution.not_placed is True and resolution.discrepancy is None


def test_not_found_but_position_exists_is_ambiguous() -> None:
    position = Position(
        symbol="SOL-USDT", side=TradeSide.LONG, quantity=D("1"), entry_price=D("1"),
        mark_price=D("1"), leverage=10, unrealized_pnl=D("0"), margin=D("0"),
    )
    resolution = resolve_entry(_unresolved(11), None, not_found=True, position=position)
    assert resolution.not_placed is False
    assert resolution.discrepancy is not None
    assert resolution.discrepancy.kind is ReconciliationKind.AMBIGUOUS


# --- 29.09: LINK #3 закрыта ручным стопом владельца (живые ответы) ------------

LINK_STOP = "2104122758140616705"
LINK_TAKE = "2104122758140616704"
LINK_ENTRY = "2104122757776154624"
LINK_MANUAL_CHILD = "2104784078754553856"
LINK_MANUAL_PARENT = "2104641198668472320"


def _link_trade(**overrides: object) -> BotTradeSnapshot:
    fields: dict[str, object] = {
        "trade_id": 3, "symbol": "LINK-USDT", "side": TradeSide.LONG,
        "open_quantity": D("2037.8"),
        "opened_at": datetime(2026, 9, 27, 8, 15, 32, 828000, tzinfo=UTC),
        "stop_order_id": LINK_STOP, "take_order_id": LINK_TAKE,
        "recorded_order_ids": frozenset({LINK_ENTRY}),
    }
    fields.update(overrides)
    return BotTradeSnapshot(**fields)  # type: ignore[arg-type]


def _link_orders():  # type: ignore[no-untyped-def]
    return [
        _parse_history_order(item)
        for item in live_items("allOrders LINK", LINK_MANUAL_STOP)
    ]


def _manual_child():  # type: ignore[no-untyped-def]
    return next(o for o in _link_orders() if o.order_id == LINK_MANUAL_CHILD)


def test_lock_link_manual_stop_fixture_shape() -> None:
    """Живая форма 29.09: наши SL/TP отменены биржей в секунду закрытия,
    дочерний ордер ручного стопа — STOP_MARKET reduceOnly с чужим
    triggerOrderId; самого ручного условника в allOrders нет."""
    by_id = {o.order_id: o for o in _link_orders()}
    assert set(by_id) == {LINK_ENTRY, LINK_STOP, LINK_TAKE, LINK_MANUAL_CHILD}
    assert by_id[LINK_STOP].status == by_id[LINK_TAKE].status == "CANCELLED"
    child = by_id[LINK_MANUAL_CHILD]
    assert (child.order_type, child.side, child.position_side, child.status) == (
        "STOP_MARKET", "SELL", "LONG", "FILLED",
    )
    assert child.trigger_order_id == LINK_MANUAL_PARENT and child.reduce_only
    assert child.updated_at == datetime(2026, 9, 29, 4, 3, 24, tzinfo=UTC)
    assert by_id[LINK_STOP].updated_at == child.updated_at


def test_lock_get_by_parent_id_returns_child_without_position_id() -> None:
    """GET trade/order по orderId ручного условника отдаёт дочерний ордер,
    positionID в этом ответе 0 → None (в allOrders у него настоящий)."""
    [item] = live_items(f"order by parent {LINK_MANUAL_PARENT}", LINK_MANUAL_STOP)
    order = _parse_history_order(item)
    assert order.order_id == LINK_MANUAL_CHILD
    assert order.trigger_order_id == LINK_MANUAL_PARENT
    assert order.position_id is None
    assert _manual_child().position_id is not None


def test_link_manual_stop_is_closed_outside_bot() -> None:
    decision = decide_trade(_link_trade(), None, _link_orders())

    assert decision.discrepancy is None
    assert decision.closes_fully
    [exit_fill] = decision.exits
    assert exit_fill.order_id == LINK_MANUAL_CHILD
    assert exit_fill.reason == MANUAL_STOP_REASON
    assert exit_fill.kind is ReconciliationKind.CLOSED_OUTSIDE_BOT
    assert exit_fill.price == D("14.776")
    assert exit_fill.quantity == D("2037.8")
    assert exit_fill.fee == D("15.055386")
    assert exit_fill.realized_pnl == D("765.8499")
    assert exit_fill.executed_at == datetime(2026, 9, 29, 4, 3, 24, tzinfo=UTC)


def test_manual_take_profit_is_closed_outside_bot() -> None:
    child = replace(_manual_child(), order_type="TAKE_PROFIT_MARKET")
    decision = decide_trade(_link_trade(), None, [child])
    assert [(e.reason, e.kind) for e in decision.exits] == [
        (MANUAL_TAKE_REASON, ReconciliationKind.CLOSED_OUTSIDE_BOT)
    ]


def test_manual_stop_limit_is_closed_outside_bot() -> None:
    child = replace(_manual_child(), order_type="STOP")
    decision = decide_trade(_link_trade(), None, [child])
    assert [e.reason for e in decision.exits] == [MANUAL_STOP_REASON]


def test_manual_stop_wrong_quantity_is_quantity_mismatch() -> None:
    child = replace(_manual_child(), executed_qty=D("1000"))
    decision = decide_trade(_link_trade(), None, [child])
    assert decision.exits == []
    assert decision.discrepancy is not None
    assert decision.discrepancy.kind is ReconciliationKind.QUANTITY_MISMATCH


@pytest.mark.parametrize("order_type", ["TRAILING_STOP_MARKET", "LIQUIDATION"])
def test_lock_unknown_conditional_child_stays_ambiguous(order_type: str) -> None:
    child = replace(_manual_child(), order_type=order_type)
    decision = decide_trade(_link_trade(), None, [child])
    assert decision.exits == []
    assert decision.discrepancy is not None
    assert decision.discrepancy.kind is ReconciliationKind.AMBIGUOUS
    assert decision.discrepancy.dedup_key == f"ambiguous:3:{LINK_MANUAL_CHILD}"


def test_lock_manual_stop_without_reduce_only_stays_ambiguous() -> None:
    child = replace(_manual_child(), reduce_only=False)
    decision = decide_trade(_link_trade(), None, [child])
    assert decision.discrepancy is not None
    assert decision.discrepancy.kind is ReconciliationKind.AMBIGUOUS


@pytest.mark.parametrize(
    "change", [{"position_side": "SHORT"}, {"side": "BUY"}, {"status": "CANCELLED"}]
)
def test_lock_manual_stop_other_side_or_status_is_not_candidate(
    change: dict[str, str],
) -> None:
    child = replace(_manual_child(), **change)
    decision = decide_trade(_link_trade(), None, [child])
    assert decision.exits == []
    assert decision.discrepancy is not None
    assert decision.discrepancy.kind is ReconciliationKind.QUANTITY_MISMATCH


def test_lock_bot_stop_trigger_still_stop_loss() -> None:
    """Дочерний ордер нашего стопа (triggerOrderId = …705) — по-прежнему
    «Стоп-лосс на бирже», не «изменённый вручную»."""
    child = replace(_manual_child(), trigger_order_id=LINK_STOP)
    decision = decide_trade(_link_trade(), None, [child])
    assert [(e.reason, e.kind) for e in decision.exits] == [
        (EXIT_STOP_LOSS, ReconciliationKind.CLOSED_STOP_LOSS)
    ]


# --- stop_missing: ручной стоп — защита, трейлинг — нет -----------------------


def _open_stop(order_type: str) -> OpenOrder:
    """Живой открытый стоп LINK 27.09, переделанный в ручной условник
    владельца: чужой orderId, стоп 14.8, заданный тип."""
    live_stop = next(
        o for o in (BingXClient._parse_open_order(i) for i in live_items("openOrders LINK"))
        if o.order_type == "STOP_MARKET"
    )
    return replace(
        live_stop, order_id=LINK_MANUAL_PARENT, order_type=order_type, stop_price=D("14.8")
    )


def _link_position() -> Position:
    position = exchange_position(_live_positions(), "LINK-USDT", TradeSide.LONG)
    assert position is not None
    return position


def test_manual_stop_limit_counts_as_protection() -> None:
    assert stop_missing(_link_trade(), _link_position(), [_open_stop("STOP")]) is None


def test_lock_manual_stop_market_counts_as_protection() -> None:
    assert stop_missing(_link_trade(), _link_position(), [_open_stop("STOP_MARKET")]) is None


def test_lock_trailing_stop_is_not_protection() -> None:
    found = stop_missing(
        _link_trade(), _link_position(), [_open_stop("TRAILING_STOP_MARKET")]
    )
    assert found is not None and found.kind is ReconciliationKind.STOP_MISSING
