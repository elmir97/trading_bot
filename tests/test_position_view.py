"""Экран «Позиции» и синхронизация уровней (этап 3, app/execution/
position_view.py): стоп/тейк из openOrders, closePosition — «на всю позицию»
(quantity формальный), связь с журналом, правила переноса уровней в журнал.
Без сети и БД."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.database.models.trade import Trade
from app.exchanges.base import OpenOrder, Position
from app.execution.position_view import (
    ProtectiveOrder,
    apply_exchange_levels,
    build_views,
    journal_only,
    link_trade,
    protective_orders,
    render_position,
    tracks_exchange,
)
from app.trading.enums import TradeSide, TradeSource, TradeStatus

D = Decimal
NOW = datetime(2026, 10, 2, 6, 30, tzinfo=UTC)


def _position(**overrides: object) -> Position:
    fields: dict[str, object] = dict(
        symbol="XRP-USDT", side=TradeSide.LONG, quantity=D(30), entry_price=D("1.5253"),
        mark_price=D("1.5263"), leverage=20, unrealized_pnl=D("0.03"),
        liquidation_price=D("1.4553"), position_id="2105907655281221634",
    )
    fields.update(overrides)
    return Position(**fields)  # type: ignore[arg-type]


def _order(order_type: str, stop: str, **overrides: object) -> OpenOrder:
    fields: dict[str, object] = dict(
        order_id="2105910661355233280", client_order_id="", symbol="XRP-USDT", side="SELL",
        position_side="LONG", order_type=order_type, quantity=D(40), executed_qty=D(0),
        price=D(0), stop_price=D(stop), status="NEW", leverage=20, reduce_only=True,
        close_position=True, working_type="MARK_PRICE", created_at=NOW, updated_at=NOW,
        take_profit=None, stop_loss=None,
    )
    fields.update(overrides)
    return OpenOrder(**fields)  # type: ignore[arg-type]


def _trade(**overrides: object) -> Trade:
    fields: dict[str, object] = dict(
        id=12, user_id=1, symbol="XRP-USDT", side=TradeSide.LONG, entry_price=D("1.5253"),
        quantity=D(40), status=TradeStatus.OPEN, source=TradeSource.IMPORTED,
        opened_at=NOW - timedelta(hours=1), stop_loss=None, take_profit=None,
        initial_stop_loss=None, external_position_id=None,
    )
    fields.update(overrides)
    return Trade(**fields)  # type: ignore[arg-type]


# --- стоп и тейк из openOrders --------------------------------------------------


class TestProtectiveOrders:
    def test_close_position_order_has_no_quantity(self) -> None:
        stops, takes = protective_orders(_position(), [_order("STOP_MARKET", "1.5241")])
        (stop,) = stops
        assert takes == ()
        assert stop.close_position and stop.quantity is None
        assert stop.trigger_price == D("1.5241") and stop.order_id == "2105910661355233280"

    def test_quantity_order_keeps_quantity(self) -> None:
        stops, _ = protective_orders(
            _position(), [_order("STOP_MARKET", "1.47", close_position=False)]
        )
        assert stops[0].quantity == D(40) and not stops[0].close_position

    def test_only_closing_side_of_this_position(self) -> None:
        orders = [
            _order("STOP_MARKET", "1.50", symbol="DOGE-USDT"),           # чужой символ
            _order("STOP_MARKET", "1.50", position_side="SHORT"),        # другая позиция
            _order("STOP_MARKET", "1.60", side="BUY"),                   # не закрытие LONG
            _order("LIMIT", "1.50", stop_price=None),                    # без триггера
            _order("TAKE_PROFIT_MARKET", "1.55"),
        ]
        stops, takes = protective_orders(_position(), orders)
        assert stops == () and [t.trigger_price for t in takes] == [D("1.55")]

    def test_short_position_closing_side_is_buy(self) -> None:
        order = _order("STOP_MARKET", "1.56", side="BUY", position_side="SHORT")
        stops, _ = protective_orders(_position(side=TradeSide.SHORT), [order])
        assert len(stops) == 1

    def test_sorted_from_nearest_to_mark(self) -> None:
        orders = [_order("STOP_MARKET", "1.40", order_id="a"),
                  _order("STOP_MARKET", "1.52", order_id="b")]
        stops, _ = protective_orders(_position(), orders)
        assert [s.order_id for s in stops] == ["b", "a"]

    def test_order_id_is_string(self) -> None:
        order = _order("STOP", "1.5", order_id=2105910661355233280)
        stops, _ = protective_orders(_position(), [order])
        assert stops[0].order_id == "2105910661355233280"


class TestRender:
    def test_close_position_shown_as_whole_position(self) -> None:
        orders = [_order("STOP_MARKET", "1.5241"), _order("TAKE_PROFIT_MARKET", "1.5287")]
        (view,) = build_views([_position()], orders, [])
        text = render_position(view, 4)
        assert "Стоп: 1.5241 (на всю позицию)" in text
        assert "Тейк: 1.5287 (на всю позицию)" in text
        assert "40" not in text  # формальный quantity closePosition-ордера не показан
        assert "⚠️ Не в журнале" in text and "Ликвидация: 1.4553" in text

    def test_quantity_order_shows_volume(self) -> None:
        order = _order("STOP_MARKET", "1.47", close_position=False)
        (view,) = build_views([_position()], [order], [])
        assert "Стоп: 1.47 (40)" in render_position(view, 4)

    def test_no_orders_and_ladder(self) -> None:
        orders = [_order("STOP_MARKET", "1.50", order_id="a"),
                  _order("STOP_MARKET", "1.45", order_id="b", close_position=False)]
        (view,) = build_views([_position()], orders, [])
        text = render_position(view, 4)
        assert "Стоп: 2 ордера: 1.5 (на всю позицию); 1.45 (40)" in text
        assert "Тейк: нет" in text
        assert view.stop is None  # лестница — нет «единственного» стопа

    def test_linked_trade_is_named(self) -> None:
        (view,) = build_views([_position()], [], [_trade()])
        assert "📒 В журнале: сделка #12" in render_position(view, 4)


class TestLinkTrade:
    def test_by_symbol_and_side(self) -> None:
        assert link_trade(_position(), [_trade(side=TradeSide.SHORT)]) is None
        assert link_trade(_position(), [_trade(symbol="DOGE-USDT")]) is None
        assert link_trade(_position(), [_trade()]).id == 12  # type: ignore[union-attr]

    def test_position_id_wins_over_newest(self) -> None:
        old = _trade(id=1, external_position_id="2105907655281221634",
                     opened_at=NOW - timedelta(days=3))
        new = _trade(id=2, opened_at=NOW)
        assert link_trade(_position(), [old, new]).id == 1  # type: ignore[union-attr]
        assert link_trade(_position(position_id=None), [old, new]).id == 2  # type: ignore[union-attr]

    def test_journal_only_excludes_linked(self) -> None:
        linked, manual = _trade(id=1), _trade(id=2, symbol="ETH-USDT")
        views = build_views([_position()], [], [linked, manual])
        assert journal_only([linked, manual], views) == [manual]


class TestTracksExchange:
    def test_rules(self) -> None:
        assert tracks_exchange(_trade(), D(40), _position())
        assert tracks_exchange(_trade(external_position_id="1"), D(40), None)
        assert not tracks_exchange(_trade(), D(40), None)          # ручная запись без биржи
        assert not tracks_exchange(_trade(), D(0), _position())    # объёма в журнале нет


def _stop(price: str) -> ProtectiveOrder:
    return ProtectiveOrder("1", D(price), True, None, "MARK_PRICE")


class TestApplyExchangeLevels:
    def test_first_stop_becomes_initial(self) -> None:
        trade = _trade()
        changes = apply_exchange_levels(trade, (_stop("1.4795"),), ())
        assert trade.stop_loss == D("1.4795") and trade.initial_stop_loss == D("1.4795")
        assert changes == ["стоп None → 1.4795"]

    def test_moved_stop_keeps_original_as_initial(self) -> None:
        trade = _trade(stop_loss=D("1.4795"), sl_approach_notified_at=NOW)
        apply_exchange_levels(trade, (_stop("1.5253"),), ())
        assert trade.stop_loss == D("1.5253")
        assert trade.initial_stop_loss == D("1.4795")
        assert trade.risk_stop == D("1.4795")
        assert trade.sl_approach_notified_at is None  # новый уровень — новая отметка

    def test_existing_initial_is_never_overwritten(self) -> None:
        trade = _trade(stop_loss=D("1.50"), initial_stop_loss=D("1.45"))
        apply_exchange_levels(trade, (_stop("1.52"),), ())
        assert trade.initial_stop_loss == D("1.45")

    def test_same_level_is_noop(self) -> None:
        trade = _trade(stop_loss=D("1.4795"), sl_approach_notified_at=NOW)
        assert apply_exchange_levels(trade, (_stop("1.4795"),), ()) == []
        assert trade.sl_approach_notified_at == NOW and trade.initial_stop_loss is None

    def test_ladder_and_missing_do_not_touch(self) -> None:
        trade = _trade(stop_loss=D("1.48"), take_profit=D("1.6"))
        assert apply_exchange_levels(trade, (_stop("1.47"), _stop("1.46")), ()) == []
        assert trade.stop_loss == D("1.48") and trade.take_profit == D("1.6")

    def test_take_follows_exchange(self) -> None:
        trade = _trade(take_profit=D("1.6"), tp_approach_notified_at=NOW)
        apply_exchange_levels(trade, (), (_stop("1.5287"),))
        assert trade.take_profit == D("1.5287") and trade.tp_approach_notified_at is None
        assert trade.initial_stop_loss is None


@pytest.mark.parametrize(
    ("initial", "stop", "expected"),
    [(None, "95", D(95)), ("95", "100", D(95)), (None, None, None)],
)
def test_risk_stop_prefers_initial(initial: str | None, stop: str | None, expected) -> None:  # type: ignore[no-untyped-def]
    trade = _trade(
        initial_stop_loss=D(initial) if initial else None, stop_loss=D(stop) if stop else None
    )
    assert trade.risk_stop == expected
