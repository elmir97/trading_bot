"""К3 (27.10, владелец): начальный стоп хранится как факт и тогда, когда он не
в убытке (безубыток, прибыль). Везде, где из стопа считается 1R, такой стоп
риска не задаёт: R «не определён», без |вход − стоп| и без деления на ноль.
Места: calculations.risk_per_unit (общее правило), закрытие сделки
(TradeJournal.recalculate), разбор ошибок (insights._realized_r), статистика
(«Средний R»), карточка сделки, сверка PnL с биржей (_check_pnl), экран
действий и Mini App (one_r_per_unit, pnl_r_text), уведомления о приближении
(risk_unit)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.analysis.insights.loader import _realized_r
from app.api import actions as actions_api
from app.bot.handlers.statistics import render_statistics
from app.bot.keyboards.trade import trade_card
from app.database.models.trade import Trade, TradeFill
from app.trading.calculations import R_UNDEFINED, risk_per_unit
from app.trading.enums import FillSide, TradeSide, TradeSource, TradeStatus
from app.trading.journal import TradeJournal
from app.trading.statistics import TradeSnapshot, calculate_statistics
from app.workers.positions import risk_unit

D = Decimal
AT = datetime(2026, 10, 27, tzinfo=UTC)


@pytest.mark.parametrize(("side", "stop", "unit"), [
    (TradeSide.LONG, "90", D(10)),
    (TradeSide.SHORT, "110", D(10)),
    (TradeSide.LONG, "100", None),     # безубыток: риск 0 — не делим на ноль
    (TradeSide.LONG, "105", None),     # стоп в прибыли — не |вход − стоп|
    (TradeSide.SHORT, "100", None),
    (TradeSide.SHORT, "95", None),
    (TradeSide.LONG, None, None),
])
def test_risk_per_unit_only_loss_side(side, stop, unit) -> None:  # type: ignore[no-untyped-def]
    assert risk_per_unit(side, D(100), D(stop) if stop else None) == unit
    assert risk_unit(side, D(100), D(stop) if stop else None) == unit   # уведомления — то же


def _closed(initial: str | None, stop: str | None = "3000") -> Trade:
    trade = Trade(
        user_id=1, exchange="bingx", symbol="ETH-USDT", side=TradeSide.LONG,
        entry_price=D(3000), exit_price=D(3120), quantity=D(1), fees=D(0), leverage=1,
        stop_loss=D(stop) if stop else None,
        initial_stop_loss=D(initial) if initial else None,
        status=TradeStatus.CLOSED, source=TradeSource.IMPORTED, opened_at=AT, closed_at=AT,
        risk_reward=D("9.99"),
    )
    trade.fills = [
        TradeFill(user_id=1, exchange="bingx", fill_side=FillSide.ENTRY, price=D(3000),
                  quantity=D(1), fee=D(0), executed_at=AT),
        TradeFill(user_id=1, exchange="bingx", fill_side=FillSide.EXIT, price=D(3120),
                  quantity=D(1), fee=D(0), executed_at=AT),
    ]
    return trade


@pytest.mark.parametrize("initial", ["3000", "3050"])
def test_close_r_undefined_when_initial_not_in_loss(initial) -> None:  # type: ignore[no-untyped-def]
    trade = TradeJournal(None).recalculate(_closed(initial))  # type: ignore[arg-type]
    assert trade.pnl == D(120) and trade.risk_reward is None


def test_close_r_from_loss_side_initial() -> None:
    trade = TradeJournal(None).recalculate(_closed("2940"))  # type: ignore[arg-type]
    assert trade.risk_reward == D("2.00")


@pytest.mark.parametrize("initial", ["3000", "3050"])
def test_insights_r_undefined_when_initial_not_in_loss(initial) -> None:  # type: ignore[no-untyped-def]
    assert _realized_r(_closed(initial, stop="2940")) is None


def test_trade_card_shows_r_undefined() -> None:
    trade = _closed("3050")
    trade.risk_reward = None
    trade.pnl, trade.pnl_percent, trade.id = D(120), D("1.2"), 5
    trade.strategy, trade.mistakes = None, []
    assert f"Результат: {R_UNDEFINED}" in trade_card(trade)
    trade.risk_reward = D("2")
    assert "Результат: +2" in trade_card(trade) or "Результат: 2" in trade_card(trade)


def test_statistics_average_r_undefined() -> None:
    stats = calculate_statistics([
        TradeSnapshot(pnl=D(10), closed_at=AT, symbol="X", side="LONG", strategy=None,
                      timeframe=None, risk_reward=None, account_balance=None),
    ])
    assert stats.average_rr is None
    assert "Средний R: не определён" in render_statistics(stats, "Статистика")


def test_pnl_r_text_undefined_only_for_journal_trade() -> None:
    from tests.test_position_actions import _inputs

    inputs = _inputs(one_r_per_unit=None)
    assert actions_api.pnl_r_text(inputs, in_journal=True) == R_UNDEFINED
    assert actions_api.pnl_r_text(inputs) is None       # сделки нет — строки нет


async def test_check_pnl_skips_when_initial_not_in_loss(caplog) -> None:  # type: ignore[no-untyped-def]
    """Сверка PnL: 1R из стопа не в убытке не берётся (раньше |вход − стоп|) —
    «не хватает данных», PNL_MISMATCH не ищется."""
    from app.workers import reconciler as rec

    trade = _closed("3050")
    trade.id, trade.pnl = 5, D(120)
    trade.fills[1].exchange_realized_pnl = D(1)

    class Repo:
        def __init__(self, session) -> None:  # type: ignore[no-untyped-def]
            pass

        async def entry_for_trade(self, user_id, trade_id):  # type: ignore[no-untyped-def]
            return None

    called: list[object] = []
    worker = rec.Reconciler.__new__(rec.Reconciler)
    ctx = SimpleNamespace(session=None, user_id=1)
    mp = pytest.MonkeyPatch()
    mp.setattr(rec, "ExecutionOrderRepository", Repo)
    mp.setattr(rec, "pnl_mismatch", lambda **kw: called.append(kw))
    try:
        with caplog.at_level("INFO"):
            await worker._check_pnl(ctx, trade)  # type: ignore[arg-type]
    finally:
        mp.undo()
    assert called == []
    assert any("не хватает данных" in r.getMessage() for r in caplog.records)
