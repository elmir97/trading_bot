"""Разбор ошибок (insights) считает фактический R от исходного стопа
(Trade.risk_stop), этап 3: перенесённый в безубыток стоп не превращает
результат сделки в «R не посчитать»."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.analysis.insights.loader import _realized_r
from app.database.models.trade import Trade
from app.trading.enums import TradeSide, TradeStatus

D = Decimal


def _closed(**overrides: object) -> Trade:
    fields: dict[str, object] = dict(
        symbol="ETH-USDT", side=TradeSide.LONG, entry_price=D(3000), exit_price=D(3120),
        stop_loss=D(2940), initial_stop_loss=None, status=TradeStatus.CLOSED,
        closed_at=datetime(2026, 10, 2, tzinfo=UTC),
    )
    fields.update(overrides)
    return Trade(**fields)  # type: ignore[arg-type]


def test_breakeven_stop_uses_initial() -> None:
    trade = _closed(stop_loss=D(3000), initial_stop_loss=D(2940))
    assert _realized_r(trade) == D(2)


def test_unmoved_stop_unchanged() -> None:
    assert _realized_r(_closed()) == D(2)
