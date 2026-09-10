"""Тесты финансовых расчётов.

Самый важный тестовый файл проекта: ошибка здесь не роняет бота, а тихо
искажает всю статистику, на которую опираются торговые решения.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.trading.calculations import (
    CalculationError,
    FillData,
    aggregate_fills,
    calculate_pnl,
    calculate_pnl_percent,
    calculate_pnl_percent_of_balance,
    calculate_position_size,
    calculate_realized_rr,
    calculate_risk_amount,
    calculate_risk_reward,
    realized_pnl_from_fills,
    stop_distance,
    to_decimal,
    weighted_average_price,
)
from app.trading.enums import FillSide, TradeSide

D = Decimal


class TestToDecimal:
    def test_float_does_not_leak_binary_noise(self) -> None:
        """Decimal(0.1) даёт мусорный хвост — конвертируем через str."""
        assert to_decimal(0.1) == D("0.1")

    def test_comma_separator(self) -> None:
        assert to_decimal("1,5") == D("1.5")

    def test_rejects_garbage(self) -> None:
        with pytest.raises(CalculationError):
            to_decimal("не число")


class TestPnL:
    def test_long_profit(self) -> None:
        pnl = calculate_pnl(
            entry_price=D("100"), exit_price=D("110"),
            quantity=D("2"), side=TradeSide.LONG,
        )
        assert pnl == D("20")

    def test_long_loss(self) -> None:
        pnl = calculate_pnl(
            entry_price=D("100"), exit_price=D("95"),
            quantity=D("2"), side=TradeSide.LONG,
        )
        assert pnl == D("-10")

    def test_short_profit_on_price_drop(self) -> None:
        pnl = calculate_pnl(
            entry_price=D("100"), exit_price=D("90"),
            quantity=D("2"), side=TradeSide.SHORT,
        )
        assert pnl == D("20")

    def test_short_loss_on_price_rise(self) -> None:
        pnl = calculate_pnl(
            entry_price=D("100"), exit_price=D("105"),
            quantity=D("2"), side=TradeSide.SHORT,
        )
        assert pnl == D("-10")

    def test_fees_reduce_result(self) -> None:
        pnl = calculate_pnl(
            entry_price=D("100"), exit_price=D("110"),
            quantity=D("2"), side=TradeSide.LONG, fees=D("1.5"),
        )
        assert pnl == D("18.5")

    def test_fees_deepen_loss(self) -> None:
        """Комиссия вычитается всегда, а не «уменьшает по модулю»."""
        pnl = calculate_pnl(
            entry_price=D("100"), exit_price=D("95"),
            quantity=D("2"), side=TradeSide.LONG, fees=D("1"),
        )
        assert pnl == D("-11")

    def test_leverage_does_not_affect_pnl(self) -> None:
        """Главный регрессионный тест: плечо в формуле PnL отсутствует.

        Позиция 0.1 BTC даёт один и тот же результат в долларах независимо
        от того, взята она с плечом 1 или 20 — меняется только маржа.
        """
        pnl = calculate_pnl(
            entry_price=D("100000"), exit_price=D("102000"),
            quantity=D("0.1"), side=TradeSide.LONG,
        )
        assert pnl == D("200")

    def test_breakeven(self) -> None:
        pnl = calculate_pnl(
            entry_price=D("100"), exit_price=D("100"),
            quantity=D("5"), side=TradeSide.LONG,
        )
        assert pnl == D("0")

    def test_rejects_non_positive_price(self) -> None:
        with pytest.raises(CalculationError):
            calculate_pnl(
                entry_price=D("0"), exit_price=D("10"),
                quantity=D("1"), side=TradeSide.LONG,
            )

    def test_precision_on_small_prices(self) -> None:
        """Монеты вида SHIB: цена с восемью нулями после запятой."""
        pnl = calculate_pnl(
            entry_price=D("0.00001234"), exit_price=D("0.00001300"),
            quantity=D("1000000"), side=TradeSide.LONG,
        )
        assert pnl == D("0.66")


class TestPnLPercent:
    def test_percent_of_margin_without_leverage(self) -> None:
        pct = calculate_pnl_percent(
            pnl=D("20"), entry_price=D("100"), quantity=D("2"), leverage=1
        )
        assert pct == D("10.0000")

    def test_leverage_amplifies_percent(self) -> None:
        """Тот же PnL при плече 10 — 100% от вложенной маржи."""
        pct = calculate_pnl_percent(
            pnl=D("20"), entry_price=D("100"), quantity=D("2"), leverage=10
        )
        assert pct == D("100.0000")

    def test_percent_of_balance(self) -> None:
        pct = calculate_pnl_percent_of_balance(
            pnl=D("-200"), account_balance=D("10000")
        )
        assert pct == D("-2.0000")


class TestStopDistance:
    def test_long(self) -> None:
        assert stop_distance(
            entry_price=D("100.50"), stop_loss=D("98.50"), side=TradeSide.LONG
        ) == D("2.00")

    def test_short(self) -> None:
        assert stop_distance(
            entry_price=D("100"), stop_loss=D("102"), side=TradeSide.SHORT
        ) == D("2")

    def test_long_with_stop_above_entry_is_rejected(self) -> None:
        """Стоп выше входа для лонга — ошибка ввода, а не отрицательный риск."""
        with pytest.raises(CalculationError, match="ниже"):
            stop_distance(
                entry_price=D("100"), stop_loss=D("102"), side=TradeSide.LONG
            )

    def test_short_with_stop_below_entry_is_rejected(self) -> None:
        with pytest.raises(CalculationError, match="выше"):
            stop_distance(
                entry_price=D("100"), stop_loss=D("98"), side=TradeSide.SHORT
            )

    def test_stop_equal_to_entry_is_rejected(self) -> None:
        with pytest.raises(CalculationError):
            stop_distance(
                entry_price=D("100"), stop_loss=D("100"), side=TradeSide.LONG
            )


class TestRiskReward:
    def test_methodology_example(self) -> None:
        """Пример из методички: вход 100.50, стоп 98.50, цель 104.50 = 1:2."""
        rr = calculate_risk_reward(
            entry_price=D("100.50"), stop_loss=D("98.50"),
            take_profit=D("104.50"), side=TradeSide.LONG,
        )
        assert rr == D("2.00")

    def test_short_rr(self) -> None:
        rr = calculate_risk_reward(
            entry_price=D("100"), stop_loss=D("102"),
            take_profit=D("94"), side=TradeSide.SHORT,
        )
        assert rr == D("3.00")

    def test_take_profit_on_wrong_side_rejected(self) -> None:
        with pytest.raises(CalculationError, match="выше"):
            calculate_risk_reward(
                entry_price=D("100"), stop_loss=D("98"),
                take_profit=D("99"), side=TradeSide.LONG,
            )

    def test_risk_amount(self) -> None:
        amount = calculate_risk_amount(
            entry_price=D("100"), stop_loss=D("98"),
            quantity=D("50"), side=TradeSide.LONG,
        )
        assert amount == D("100")


class TestRealizedRR:
    def test_stop_hit_gives_minus_one_r(self) -> None:
        r = calculate_realized_rr(
            entry_price=D("100"), stop_loss=D("98"),
            exit_price=D("98"), side=TradeSide.LONG,
        )
        assert r == D("-1.00")

    def test_two_r_win(self) -> None:
        r = calculate_realized_rr(
            entry_price=D("100"), stop_loss=D("98"),
            exit_price=D("104"), side=TradeSide.LONG,
        )
        assert r == D("2.00")

    def test_short_two_r_win(self) -> None:
        r = calculate_realized_rr(
            entry_price=D("100"), stop_loss=D("102"),
            exit_price=D("96"), side=TradeSide.SHORT,
        )
        assert r == D("2.00")


class TestPositionSizing:
    def test_basic_case(self) -> None:
        """Баланс 10000, риск 1%, вход 100, стоп 98 → риск 100$, объём 50."""
        sizing = calculate_position_size(
            account_balance=D("10000"), risk_percent=D("1"),
            entry_price=D("100"), stop_loss=D("98"), side=TradeSide.LONG,
        )
        assert sizing.risk_amount == D("100")
        assert sizing.quantity == D("50")
        assert sizing.position_value == D("5000")
        assert sizing.stop_distance_percent == D("2.0000")

    def test_tighter_stop_allows_bigger_position(self) -> None:
        """Ядро методологии: короткий стоп на ретесте даёт больший объём
        при том же риске в процентах."""
        wide = calculate_position_size(
            account_balance=D("10000"), risk_percent=D("2"),
            entry_price=D("100"), stop_loss=D("95"), side=TradeSide.LONG,
        )
        tight = calculate_position_size(
            account_balance=D("10000"), risk_percent=D("2"),
            entry_price=D("100"), stop_loss=D("99"), side=TradeSide.LONG,
        )
        assert tight.quantity > wide.quantity
        assert tight.risk_amount == wide.risk_amount

    def test_required_margin_depends_on_leverage(self) -> None:
        sizing = calculate_position_size(
            account_balance=D("10000"), risk_percent=D("2"),
            entry_price=D("100"), stop_loss=D("98"), side=TradeSide.LONG,
        )
        assert sizing.position_value == D("10000")
        assert sizing.required_margin(10) == D("1000")
        assert sizing.required_margin(1) == D("10000")

    def test_rejects_risk_above_hundred(self) -> None:
        with pytest.raises(CalculationError):
            calculate_position_size(
                account_balance=D("10000"), risk_percent=D("101"),
                entry_price=D("100"), stop_loss=D("98"), side=TradeSide.LONG,
            )

    def test_rejects_zero_balance(self) -> None:
        with pytest.raises(CalculationError):
            calculate_position_size(
                account_balance=D("0"), risk_percent=D("2"),
                entry_price=D("100"), stop_loss=D("98"), side=TradeSide.LONG,
            )


class TestFillAggregation:
    def test_weighted_average_is_not_arithmetic_mean(self) -> None:
        fills = [
            FillData(FillSide.ENTRY, D("100000"), D("0.1")),
            FillData(FillSide.ENTRY, D("90000"), D("0.9")),
        ]
        assert weighted_average_price(fills) == D("91000")

    def test_single_entry_single_exit(self) -> None:
        agg = aggregate_fills([
            FillData(FillSide.ENTRY, D("100"), D("1"), D("0.1")),
            FillData(FillSide.EXIT, D("110"), D("1"), D("0.11")),
        ])
        assert agg.entry_price == D("100")
        assert agg.exit_price == D("110")
        assert agg.open_quantity == D("0")
        assert agg.total_fees == D("0.21")
        assert agg.is_fully_closed

    def test_partial_close_leaves_open_quantity(self) -> None:
        agg = aggregate_fills([
            FillData(FillSide.ENTRY, D("100"), D("2")),
            FillData(FillSide.EXIT, D("110"), D("0.5")),
        ])
        assert agg.open_quantity == D("1.5")
        assert not agg.is_fully_closed

    def test_scaled_entry_averages_price(self) -> None:
        agg = aggregate_fills([
            FillData(FillSide.ENTRY, D("100"), D("1")),
            FillData(FillSide.ENTRY, D("102"), D("1")),
        ])
        assert agg.entry_price == D("101")
        assert agg.entry_quantity == D("2")

    def test_closing_more_than_opened_is_rejected(self) -> None:
        with pytest.raises(CalculationError, match="Закрыто больше"):
            aggregate_fills([
                FillData(FillSide.ENTRY, D("100"), D("1")),
                FillData(FillSide.EXIT, D("110"), D("2")),
            ])

    def test_realized_pnl_counts_only_closed_part(self) -> None:
        """Открытая часть позиции не даёт реализованного результата."""
        pnl = realized_pnl_from_fills(
            [
                FillData(FillSide.ENTRY, D("100"), D("2")),
                FillData(FillSide.EXIT, D("110"), D("1")),
            ],
            side=TradeSide.LONG,
        )
        assert pnl == D("10")

    def test_no_exit_means_zero_realized(self) -> None:
        pnl = realized_pnl_from_fills(
            [FillData(FillSide.ENTRY, D("100"), D("1"))], side=TradeSide.LONG
        )
        assert pnl == D("0")
