"""Тесты app/execution/leverage.py (раздел 16 ТЗ, шаг 15.5.1).

Чистая функция, без сети и БД — как и остальные предторговые решения
(sizing.py, guards.py), проверяется без моков.
"""

from __future__ import annotations

from app.exchanges.base import LeverageInfo
from app.execution.leverage import leverage_needs_update
from app.trading.enums import TradeSide


def _leverage(**overrides: object) -> LeverageInfo:
    fields: dict[str, object] = {
        "symbol": "BTC-USDT",
        "long_leverage": 20,
        "short_leverage": 20,
        "max_long_leverage": 150,
        "max_short_leverage": 150,
    }
    fields.update(overrides)
    return LeverageInfo(**fields)  # type: ignore[arg-type]


class TestLeverageNeedsUpdate:
    def test_long_matches_desired_no_update(self) -> None:
        current = _leverage(long_leverage=20, short_leverage=5)
        assert leverage_needs_update(current, desired=20, side=TradeSide.LONG) is False

    def test_long_differs_from_desired_needs_update(self) -> None:
        current = _leverage(long_leverage=20, short_leverage=5)
        assert leverage_needs_update(current, desired=30, side=TradeSide.LONG) is True

    def test_short_matches_desired_no_update(self) -> None:
        current = _leverage(long_leverage=5, short_leverage=20)
        assert leverage_needs_update(current, desired=20, side=TradeSide.SHORT) is False

    def test_short_differs_from_desired_needs_update(self) -> None:
        current = _leverage(long_leverage=5, short_leverage=20)
        assert leverage_needs_update(current, desired=30, side=TradeSide.SHORT) is True

    def test_compares_by_requested_side_not_either(self) -> None:
        """Хедж-режим: long и short независимы. long уже совпадает с
        желаемым, но спрашиваем про short — должно решить по short,
        не подхватить совпадение long."""
        current = _leverage(long_leverage=20, short_leverage=5)
        assert leverage_needs_update(current, desired=20, side=TradeSide.SHORT) is True

    def test_other_side_mismatch_does_not_affect_result(self) -> None:
        """И наоборот: short не совпадает с желаемым, но спрашиваем про
        long, который совпадает — не должно ложно сказать "нужно менять"
        из-за несовпадения другой стороны."""
        current = _leverage(long_leverage=20, short_leverage=99)
        assert leverage_needs_update(current, desired=20, side=TradeSide.LONG) is False
