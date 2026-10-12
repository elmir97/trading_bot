"""Шаг цены (app/execution/price_step.py, round_inputs в opening/calc.py) — A.4.

Правило владельца 12.10: округление никогда не увеличивает риск в $ сверх
выбранного. Открытие (объём после округления): стоп дальше, тейк ближе, лимит
выгоднее — LONG вниз, SHORT вверх. Этап 4 (объём фиксирован): стоп ближе."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from app.exchanges.base import SymbolInfo
from app.execution.opening.calc import OpeningInputs, round_inputs
from app.execution.price_step import (
    on_step,
    opening_level,
    price_step,
    stop_closer,
    take_closer,
)
from app.trading.enums import EntryType, TradeSide

D = Decimal
LONG, SHORT = TradeSide.LONG, TradeSide.SHORT
XRP = SymbolInfo("XRP-USDT", 4, 0, D(2), D(2))


class TestStep:
    @pytest.mark.parametrize(
        ("precision", "step"), [(4, D("0.0001")), (0, D(1)), (8, D("0.00000001")), (1, D("0.1"))]
    )
    def test_price_step(self, precision: int, step: Decimal) -> None:
        assert price_step(precision) == step

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(D("1.4400"), True), (D("1.44"), True), (D("1.44002"), False), (D("2"), True)],
    )
    def test_on_step(self, value: Decimal, expected: bool) -> None:
        assert on_step(value, D("0.0001")) is expected


class TestOpeningLevel:
    @pytest.mark.parametrize(
        ("side", "value", "expected"),
        [
            # LONG: стоп 1.36006 — дальше от входа (вниз); тейк 1.44002 — ближе (вниз);
            # лимит 1.39507 — дешевле (вниз)
            (LONG, D("1.36006"), D("1.3600")),
            (LONG, D("1.44002"), D("1.4400")),
            (LONG, D("1.39507"), D("1.3950")),
            # SHORT — зеркально вверх
            (SHORT, D("1.44002"), D("1.4401")),
            (SHORT, D("1.36006"), D("1.3601")),
            # уже на шаге — без изменений
            (LONG, D("1.3600"), D("1.3600")),
            (SHORT, D("1.3600"), D("1.3600")),
        ],
    )
    def test_direction(self, side: TradeSide, value: Decimal, expected: Decimal) -> None:
        assert opening_level(value, side, D("0.0001")) == expected

    def test_precision_zero(self) -> None:
        assert opening_level(D("60123.7"), LONG, D(1)) == D(60123)
        assert opening_level(D("60123.2"), SHORT, D(1)) == D(60124)


class TestStageFour:
    def test_take_closer(self) -> None:
        assert take_closer(D("1.44007"), LONG, D("0.0001")) == D("1.4400")
        assert take_closer(D("1.36003"), SHORT, D("0.0001")) == D("1.3601")

    def test_stop_closer_fixed_quantity(self) -> None:
        # Объём фиксирован — стоп ближе к цене: LONG вверх, SHORT вниз.
        assert stop_closer(D("1.36003"), LONG, D("0.0001")) == D("1.3601")
        assert stop_closer(D("1.44007"), SHORT, D("0.0001")) == D("1.4400")


INPUTS = OpeningInputs(
    symbol="XRP-USDT", side=LONG, entry_type=EntryType.MARKET,
    stop_loss=D("1.36006"), take_profit=D("1.44002"), risk_percent=D(1), leverage=10,
)


class TestRoundInputs:
    def test_long_market(self) -> None:
        rounded, changes = round_inputs(INPUTS, XRP)
        assert (rounded.stop_loss, rounded.take_profit) == (D("1.3600"), D("1.4400"))
        assert [(c.field, c.label, c.before, c.after) for c in changes] == [
            ("stop_loss", "стоп", D("1.36006"), D("1.3600")),
            ("take_profit", "тейк", D("1.44002"), D("1.4400")),
        ]
        # остальное — как было
        back = replace(rounded, stop_loss=INPUTS.stop_loss, take_profit=INPUTS.take_profit)
        assert back == INPUTS

    def test_short_limit(self) -> None:
        inputs = replace(
            INPUTS, side=SHORT, entry_type=EntryType.LIMIT, limit_price=D("1.40003"),
            stop_loss=D("1.44002"), take_profit=D("1.36006"),
        )
        rounded, changes = round_inputs(inputs, XRP)
        assert rounded.limit_price == D("1.4001")
        assert rounded.stop_loss == D("1.4401")
        assert rounded.take_profit == D("1.3601")
        assert [c.field for c in changes] == ["limit_price", "stop_loss", "take_profit"]

    def test_on_step_unchanged(self) -> None:
        inputs = replace(INPUTS, stop_loss=D("1.36"), take_profit=None)
        rounded, changes = round_inputs(inputs, XRP)
        assert rounded is inputs
        assert changes == ()

    def test_market_ignores_limit_price(self) -> None:
        inputs = replace(INPUTS, stop_loss=D("1.36"), take_profit=None, limit_price=D("1.40003"))
        rounded, changes = round_inputs(inputs, XRP)
        assert rounded.limit_price == D("1.40003")
        assert changes == ()
