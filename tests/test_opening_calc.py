"""Расчёт карточки открытия (app/execution/opening/calc.py) — чистые функции.

Эталон — пример карточки из docs/open-trade-plan.md §9: XRP LONG рынок 1.4950,
стоп 1.4501, тейк 1.5399, equity 1500, риск 1%, плечо 10x, тейкер 0.05%."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from app.core.numfmt import fmt_price
from app.exchanges.base import MarginType, Position, SymbolInfo
from app.execution.opening.calc import (
    Level,
    Limits,
    MarketSnapshot,
    OpeningInputs,
    compute,
    floor_to_step,
    liquidation_estimate,
    max_leverage_for_stop,
)
from app.trading.enums import EntryType, TradeSide

D = Decimal

INFO = SymbolInfo("XRP-USDT", 4, 0, D(2), D(2))
MARKET = MarketSnapshot(
    last_price=D("1.4950"), symbol_info=INFO, equity=D(1500), available=D(1475),
    taker_rate=D("0.0005"), max_leverage=125, margin_type=MarginType.ISOLATED,
    desired_margin_type=MarginType.ISOLATED, position=None, symbol_busy=False,
)
LIMITS = Limits(
    plan_risk_percent=D(2), plan_max_leverage=20, min_stop_distance_percent=D("0.1"),
    liq_buffer=D("1.5"), mmr=D("0.01"),
)
INPUTS = OpeningInputs(
    symbol="XRP-USDT", side=TradeSide.LONG, entry_type=EntryType.MARKET,
    stop_loss=D("1.4501"), take_profit=D("1.5399"), risk_percent=D(1), leverage=10,
)


def _codes(calc) -> set[str]:  # type: ignore[no-untyped-def]
    return {i.code for i in calc.issues}


class TestHelpers:
    @pytest.mark.parametrize(
        ("value", "step", "expected"),
        [
            (D("323.97"), D(1), D(323)),
            (D("0.01999"), D("0.001"), D("0.019")),
            (D("37"), D(10), D(30)),
            (D("-1"), D(1), D(0)),
            (D(0), D(1), D(0)),
        ],
    )
    def test_floor_to_step(self, value: Decimal, step: Decimal, expected: Decimal) -> None:
        assert floor_to_step(value, step) == expected

    def test_liquidation_isolated(self) -> None:
        assert liquidation_estimate(D("1.495"), TradeSide.LONG, 10, D("0.01")) == D("1.36045")
        assert liquidation_estimate(D(100), TradeSide.SHORT, 10, D("0.01")) == D("109.00")

    def test_max_leverage_for_stop(self) -> None:
        # стоп 3%: 1 / (1.5·0.03 + 0.01) = 18.18 → 18 (пример из отказа §1.4)
        assert max_leverage_for_stop(D("0.03"), D("1.5"), D("0.01")) == 18
        # очень широкий стоп — не меньше 1
        assert max_leverage_for_stop(D("0.9"), D("1.5"), D("0.01")) == 1


class TestCardExample:
    def test_numbers_match_plan_card(self) -> None:
        calc = compute(INPUTS, MARKET, LIMITS)
        assert calc.quantity == D(323)
        assert calc.notional == D("482.8850")
        assert calc.risk_usd.quantize(D("0.01")) == D("14.98")
        assert calc.fee_usd.quantize(D("0.01")) == D("0.48")
        assert calc.margin.quantize(D("0.01")) == D("48.29")
        assert calc.liq_estimate is not None
        assert fmt_price(calc.liq_estimate, 4) == "1.3605"   # как на карточке (HALF_UP)
        assert calc.liq_ratio is not None and calc.liq_ratio > D("2.99")
        assert calc.rr is not None and calc.rr.quantize(D("0.01")) == D("1.00")
        assert calc.max_leverage_for_stop == 18
        assert calc.suggested_leverage == 18
        assert not calc.issues and not calc.blocked

    def test_risk_with_fee_never_exceeds_budget(self) -> None:
        for risk in (D("0.25"), D("0.5"), D(1), D("1.37"), D(2)):
            calc = compute(replace(INPUTS, risk_percent=risk), MARKET, LIMITS)
            assert calc.risk_usd <= MARKET.equity * risk / 100
            # следующий лот уже вышел бы за бюджет — округление только вниз
            per_unit = calc.risk_usd / calc.quantity
            assert (calc.quantity + 1) * per_unit > MARKET.equity * risk / 100

    def test_short_mirror(self) -> None:
        calc = compute(
            replace(INPUTS, side=TradeSide.SHORT, stop_loss=D("1.5399"), take_profit=D("1.4501")),
            MARKET, LIMITS,
        )
        assert calc.quantity > 0 and not calc.issues
        assert calc.liq_estimate is not None and calc.liq_estimate > D("1.5399")


class TestBlocks:
    def test_stop_wrong_side_of_entry(self) -> None:
        calc = compute(replace(INPUTS, stop_loss=D("1.50")), MARKET, LIMITS)
        assert "STOP_WRONG_SIDE" in _codes(calc) and calc.quantity == 0 and calc.blocked

    def test_limit_stop_vs_last_price(self) -> None:
        """Р4: стоп ниже лимита, но выше last — биржа отклонит весь вход."""
        calc = compute(
            replace(INPUTS, entry_type=EntryType.LIMIT, limit_price=D("1.60"),
                    stop_loss=D("1.55"), take_profit=None),
            MARKET, LIMITS,
        )
        issue = next(i for i in calc.issues if i.code == "STOP_WRONG_SIDE")
        assert "текущей цены 1.495" in issue.message

    def test_take_wrong_side(self) -> None:
        calc = compute(replace(INPUTS, take_profit=D("1.49")), MARKET, LIMITS)
        assert "TAKE_WRONG_SIDE" in _codes(calc)

    def test_stop_too_close(self) -> None:
        calc = compute(replace(INPUTS, stop_loss=D("1.4940")), MARKET, LIMITS)
        assert "STOP_TOO_CLOSE" in _codes(calc)

    def test_risk_above_plan(self) -> None:
        calc = compute(replace(INPUTS, risk_percent=D("2.01")), MARKET, LIMITS)
        assert "RISK_TOO_HIGH" in _codes(calc)
        ok = compute(replace(INPUTS, risk_percent=D("2.004")), MARKET, LIMITS)
        assert "RISK_TOO_HIGH" not in _codes(ok)   # сравнение с точностью показа

    def test_leverage_above_exchange(self) -> None:
        calc = compute(INPUTS, replace(MARKET, max_leverage=5), LIMITS)
        assert "LEVERAGE_ABOVE_EXCHANGE" in _codes(calc)

    def test_size_too_small(self) -> None:
        calc = compute(replace(INPUTS, risk_percent=D("0.001")), MARKET, LIMITS)
        issue = next(i for i in calc.issues if i.code == "SIZE_TOO_SMALL")
        assert "минимума биржи (2 XRP / 2.00 USDT)" in issue.message

    def test_insufficient_margin(self) -> None:
        calc = compute(INPUTS, replace(MARKET, available=D(40)), LIMITS)
        issue = next(i for i in calc.issues if i.code == "INSUFFICIENT_MARGIN")
        assert "свободно 40.00" in issue.message

    def test_available_unknown_blocks(self) -> None:
        calc = compute(INPUTS, replace(MARKET, available=None), LIMITS)
        assert "AVAILABLE_MARGIN_UNKNOWN" in _codes(calc)

    def test_non_positive_equity(self) -> None:
        calc = compute(INPUTS, replace(MARKET, equity=D(0)), LIMITS)
        assert "NON_POSITIVE_EQUITY" in _codes(calc) and calc.quantity == 0

    def test_liquidation_too_close_names_max_leverage(self) -> None:
        """§1.4: в отказе — максимальное плечо, которое пройдёт при этом стопе."""
        calc = compute(replace(INPUTS, leverage=20), MARKET, LIMITS)
        issue = next(i for i in calc.issues if i.code == "LIQ_TOO_CLOSE")
        assert "максимум 18x" in issue.message and issue.level is Level.BLOCK

    def test_max_leverage_capped_by_plan(self) -> None:
        calc = compute(
            replace(INPUTS, leverage=20), MARKET, replace(LIMITS, plan_max_leverage=12)
        )
        issue = next(i for i in calc.issues if i.code == "LIQ_TOO_CLOSE")
        assert "максимум 12x" in issue.message

    def test_crossed_has_no_pre_entry_liquidation(self) -> None:
        crossed = replace(
            MARKET, margin_type=MarginType.CROSSED, desired_margin_type=MarginType.CROSSED
        )
        calc = compute(replace(INPUTS, leverage=20), crossed, LIMITS)
        assert calc.liq_estimate is None and "LIQ_TOO_CLOSE" not in _codes(calc)

    def test_position_on_side_exists(self) -> None:
        """§1.6: только в пустую сторону."""
        position = Position("XRP-USDT", TradeSide.LONG, D(330), D("1.4952"), D("1.495"), 10, D(0))
        calc = compute(INPUTS, replace(MARKET, position=position, symbol_busy=True), LIMITS)
        issue = next(i for i in calc.issues if i.code == "POSITION_EXISTS")
        assert "уже открыт LONG 330 (вход 1.4952)" in issue.message

    def test_margin_mode_locked_when_symbol_busy(self) -> None:
        busy = replace(MARKET, margin_type=MarginType.CROSSED, symbol_busy=True)
        assert "MARGIN_MODE_LOCKED" in _codes(compute(INPUTS, busy, LIMITS))
        free = replace(MARKET, margin_type=MarginType.CROSSED, symbol_busy=False)
        assert "MARGIN_MODE_LOCKED" not in _codes(compute(INPUTS, free, LIMITS))


class TestLimit:
    def test_limit_uses_limit_price(self) -> None:
        calc = compute(
            replace(INPUTS, entry_type=EntryType.LIMIT, limit_price=D("1.4800"),
                    stop_loss=D("1.4500"), expiry_minutes=240),
            MARKET, LIMITS,
        )
        assert calc.entry_price == D("1.4800") and not calc.fills_immediately
        assert not calc.blocked

    def test_marketable_limit_flagged(self) -> None:
        calc = compute(
            replace(INPUTS, entry_type=EntryType.LIMIT, limit_price=D("1.4976")), MARKET, LIMITS
        )
        assert calc.fills_immediately

    def test_limit_without_price(self) -> None:
        inputs = replace(INPUTS, entry_type=EntryType.LIMIT, limit_price=None)
        calc = compute(inputs, MARKET, LIMITS)
        assert "INVALID_PRICE" in _codes(calc)
