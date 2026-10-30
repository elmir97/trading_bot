"""K8: net result is independent of the planned RR and never persisted."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.analysis.insights.loader import _realized_r
from app.bot.keyboards.trade import trade_card
from app.database.models.trade import Trade, TradeFill
from app.trading.enums import FillSide, TradeSide, TradeSource, TradeStatus
from app.trading.journal import TradeJournal

D = Decimal
AT = datetime(2026, 10, 30, tzinfo=UTC)


def trade(side=TradeSide.LONG, entry_fee="1", exit_fee="0.5", qty="5") -> Trade:
    price, stop, exit_price = ("100", "90", "110") if side is TradeSide.LONG else (
        "100", "110", "90"
    )
    t = Trade(
        id=1, user_id=1, symbol="ETH-USDT", side=side, source=TradeSource.MANUAL,
        status=TradeStatus.CLOSED, initial_stop_loss=D(stop), stop_loss=D(price),
        fill_confirmed=True, entry_price=D(price), quantity=D(10), leverage=1,
        opened_at=AT, closed_at=AT, risk_reward=None,
        strategy=None, mistakes=[], pnl=D(49), pnl_percent=D(1), fees=D("1.5"),
    )
    t.fills = [
        TradeFill(fill_side=FillSide.ENTRY, price=D(price), quantity=D(10),
                  fee=D(entry_fee), executed_at=AT),
        TradeFill(fill_side=FillSide.EXIT, price=D(exit_price), quantity=D(qty),
                  fee=D(exit_fee), executed_at=AT),
    ]
    return t


@pytest.mark.parametrize("side", [TradeSide.LONG, TradeSide.SHORT])
def test_partial_net_r(side) -> None:
    t = trade(side)
    assert t.realized_result.value == D("0.98")
    assert t.realized_result.reason is None
    assert _realized_r(t) == D("0.98")
    assert "0.98" in trade_card(t)
    t.risk_reward = D(99)
    assert t.realized_result.value == D("0.98")
    TradeJournal(None).recalculate(t)
    assert t.risk_reward == D(99)


def test_full_close_and_rebates() -> None:
    t = trade(entry_fee="-1", exit_fee="-0.5", qty="10")
    assert t.realized_result.value == D("1.015")


def test_zero_fee_is_known_zero() -> None:
    assert trade(entry_fee="0", exit_fee="0").realized_result.value == D(1)


def test_several_exits_allocate_entry_fee_once() -> None:
    t = trade()
    t.fills.append(TradeFill(fill_side=FillSide.EXIT, price=D(120), quantity=D(5),
                             fee=D("0.5"), executed_at=AT))
    assert t.realized_result.value == D("1.48")


@pytest.mark.parametrize("stop", [None, D(100), D(105)])
def test_unknown_risk_is_not_abs_or_zero(stop) -> None:
    t = trade()
    t.initial_stop_loss = stop
    t.stop_loss = stop
    assert t.realized_result.value is None
    assert t.realized_result.reason


def test_multiple_entries_are_insufficient_evidence_not_proven_scaling() -> None:
    t = trade()
    t.fills.append(TradeFill(fill_side=FillSide.ENTRY, price=D(120), quantity=D(5),
                             fee=D(0), executed_at=AT))
    assert t.realized_result.value is None
    assert t.realized_result.reason == "Недостаточно данных для определения базы риска"


def test_unconfirmed_or_missing_or_excess_exits() -> None:
    t = trade()
    t.fill_confirmed = False
    assert t.realized_result.value is None
    t.fill_confirmed = True
    t.fills = t.fills[:1]
    assert t.realized_result.value is None
    t = trade(qty="11")
    assert t.realized_result.value is None


def test_moved_stop_does_not_change_initial_basis() -> None:
    t = trade()
    before = t.realized_result.value
    t.stop_loss = D(108)
    assert t.realized_result.value == before == D("0.98")


def test_unknown_confirmation_and_fee_are_not_zero() -> None:
    t = trade()
    t.fill_confirmed = None  # type: ignore[assignment]
    assert t.realized_result.value is None
    t.fill_confirmed = True
    t.fills[0].fee = None  # type: ignore[assignment]
    assert t.realized_result.value is None
    assert t.realized_result.reason == "Комиссия неизвестна"


def test_exit_before_entry_is_not_a_proven_result() -> None:
    t = trade()
    t.fills[0].executed_at = AT.replace(day=31)
    assert t.realized_result.value is None
    assert t.realized_result.reason == "Недостаточно данных о порядке исполнений"
