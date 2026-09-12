"""Форматирование чисел на выводе.

fmt_price/fmt_qty/fmt_ratio округляют явным ROUND_HALF_UP: контекст
Decimal по умолчанию — ROUND_HALF_EVEN (округление к чётной цифре), и
без явного параметра 2.505 ушло бы в 2.50, а не в 2.51.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.bot.formatting import fmt_num, fmt_price, fmt_qty, fmt_ratio

D = Decimal


class TestFmtNum:
    """Формат :g не убирает хвост нулей у Decimal.

    Из-за этого проценты выводились как «2.0000%», а RR как «1:2.0000».
    """

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (D("2.0000"), "2"),
            (D("10.0000"), "10"),      # normalize() даёт 1E+1 — проверяем
            (D("1.5000"), "1.5"),
            (D("0.00001234"), "0.00001234"),
            (D("100.50"), "100.5"),
            (None, "—"),
        ],
    )
    def test_fmt_num(self, value: Decimal | None, expected: str) -> None:
        assert fmt_num(value) == expected


class TestFmtPrice:
    """Округление по precision символа, а не только обрезка нулей —
    иначе в бот уходят хвосты вроде «74077.859884955571169409993»."""

    def test_large_price_rounds_to_symbol_precision(self) -> None:
        assert fmt_price(D("74077.859884955571169409993"), 2) == "74077.86"

    def test_price_below_one_uses_given_precision(self) -> None:
        assert fmt_price(D("0.0000123456"), 6) == "0.000012"

    def test_precision_zero_rounds_to_integer(self) -> None:
        assert fmt_price(D("105.6"), 0) == "106"

    def test_negative_value(self) -> None:
        assert fmt_price(D("-45.678"), 2) == "-45.68"

    def test_none(self) -> None:
        assert fmt_price(None, 2) == "—"

    def test_half_up_not_bankers_rounding(self) -> None:
        """ROUND_HALF_EVEN дал бы «2.50» — явный ROUND_HALF_UP даёт «2.51»."""
        assert fmt_price(D("2.505"), 2) == "2.51"

    # Резервное правило (precision=None): >=1000 -> 2 знака, >=1 -> 4,
    # <1 -> 6. Тесты на границах, где правило переключается.
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (D("74077.859884955571169409993"), "74077.86"),   # >= 1000 -> 2
            (D("1000"), "1000"),                                # ровно на границе -> 2
            (D("999.99999"), "1000"),                           # < 1000 -> 4, округляется вверх
            (D("1"), "1"),                                      # ровно на границе -> 4
            (D("0.99999999"), "1"),                             # < 1 -> 6, округляется вверх
            (D("0.00001234"), "0.000012"),                      # < 1 -> 6
            (None, "—"),
        ],
    )
    def test_fallback_precision_by_magnitude(
        self, value: Decimal | None, expected: str
    ) -> None:
        assert fmt_price(value) == expected


class TestFmtQty:
    def test_default_precision_matches_previous_behaviour(self) -> None:
        """Расчёт от риска даёт периодическую дробь: 200 / 60 = 3.333…"""
        assert fmt_qty(D("3.333333333333")) == "3.33333333"

    def test_explicit_precision_from_symbol_info(self) -> None:
        assert fmt_qty(D("3.333333333333"), 3) == "3.333"

    def test_precision_zero(self) -> None:
        assert fmt_qty(D("7.6"), 0) == "8"

    def test_none(self) -> None:
        assert fmt_qty(None, 3) == "—"


class TestFmtRatio:
    """RR, RSI, объём к среднему — округление внутри функции, не по
    месту вызова."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (D("2.5"), "2.5"),
            (D("2.505"), "2.51"),      # ROUND_HALF_UP, не банковское округление
            (D("63.456"), "63.46"),
            (D("1.0050"), "1.01"),
            (D("-0.005"), "-0.01"),
            (D("2.00"), "2"),          # хвостовые нули всё ещё обрезаются
        ],
    )
    def test_rounds_to_two_digits(self, value: Decimal, expected: str) -> None:
        assert fmt_ratio(value) == expected

    def test_none(self) -> None:
        assert fmt_ratio(None) == "—"
