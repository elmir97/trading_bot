"""Тесты app/execution/sizing.py (этап 15.3, раздел 6 и раздел 13 ТЗ).

Чистые функции на Decimal — ни БД, ни сети, поэтому без async и без
DATABASE_URL: эти тесты обязаны проходить всегда, в любом окружении.
"""

from __future__ import annotations

from decimal import Decimal

from app.exchanges.base import SymbolInfo
from app.execution.models import ExecutionRefusal, ExecutionRefusalCode
from app.execution.sizing import SizingResult, calculate_size, round_levels_toward_entry
from app.trading.enums import TradeSide

D = Decimal


def _symbol_info(**overrides: object) -> SymbolInfo:
    fields: dict[str, object] = {
        "symbol": "BTC-USDT",
        "price_precision": 1,
        "quantity_precision": 2,
        "min_quantity": D("0.01"),
        "min_notional": D("0"),
    }
    fields.update(overrides)
    return SymbolInfo(**fields)  # type: ignore[arg-type]


class TestRoundsDown:
    def test_quantity_truncated_not_rounded(self) -> None:
        """risk_amount=10, дистанция=3 → 10/3 = 3.333... → вниз до 3.33,
        а не до 3.34, как дал бы обычный round()."""
        result = calculate_size(
            account_balance=D("1000"),
            risk_percent=D("1"),
            entry_price=D("100"),
            stop_loss=D("97"),
            side=TradeSide.LONG,
            leverage=1,
            symbol_info=_symbol_info(quantity_precision=2),
        )
        assert isinstance(result, SizingResult)
        assert result.quantity == D("3.33")

    def test_integer_precision_drops_fractional_part(self) -> None:
        result = calculate_size(
            account_balance=D("1000"),
            risk_percent=D("5"),
            entry_price=D("100"),
            stop_loss=D("90"),
            side=TradeSide.LONG,
            leverage=1,
            symbol_info=_symbol_info(quantity_precision=0, min_quantity=D("1")),
        )
        assert isinstance(result, SizingResult)
        # risk_amount=50, дистанция=10 → 5.0 ровно, округление вниз не меняет.
        assert result.quantity == D("5")

    def test_short_side_rounds_down_the_same_way(self) -> None:
        result = calculate_size(
            account_balance=D("1000"),
            risk_percent=D("1"),
            entry_price=D("97"),
            stop_loss=D("100"),
            side=TradeSide.SHORT,
            leverage=1,
            symbol_info=_symbol_info(quantity_precision=2),
        )
        assert isinstance(result, SizingResult)
        assert result.quantity == D("3.33")


class TestMinimumLot:
    def test_below_min_quantity_refused(self) -> None:
        result = calculate_size(
            account_balance=D("1000"),
            risk_percent=D("1"),
            entry_price=D("100"),
            stop_loss=D("97"),
            side=TradeSide.LONG,
            leverage=1,
            symbol_info=_symbol_info(quantity_precision=2, min_quantity=D("5")),
        )
        assert isinstance(result, ExecutionRefusal)
        assert result.code is ExecutionRefusalCode.SIZE_TOO_SMALL

    def test_rounds_to_zero_refused(self) -> None:
        """Риск настолько мал относительно дистанции, что объём после
        округления вниз до шага лота превращается в ровный ноль."""
        result = calculate_size(
            account_balance=D("10"),
            risk_percent=D("1"),
            entry_price=D("200"),
            stop_loss=D("100"),
            side=TradeSide.LONG,
            leverage=1,
            symbol_info=_symbol_info(quantity_precision=2, min_quantity=D("0")),
        )
        assert isinstance(result, ExecutionRefusal)
        assert result.code is ExecutionRefusalCode.SIZE_TOO_SMALL

    def test_below_min_notional_refused(self) -> None:
        result = calculate_size(
            account_balance=D("100"),
            risk_percent=D("0.1"),
            entry_price=D("10"),
            stop_loss=D("9"),
            side=TradeSide.LONG,
            leverage=1,
            symbol_info=_symbol_info(
                quantity_precision=3, min_quantity=D("0.001"), min_notional=D("50")
            ),
        )
        assert isinstance(result, ExecutionRefusal)
        assert result.code is ExecutionRefusalCode.SIZE_TOO_SMALL


class TestInsufficientMargin:
    def test_margin_exceeds_balance_refused(self) -> None:
        result = calculate_size(
            account_balance=D("100"),
            risk_percent=D("50"),
            entry_price=D("100"),
            stop_loss=D("99"),
            side=TradeSide.LONG,
            leverage=1,
            symbol_info=_symbol_info(quantity_precision=0, min_quantity=D("1")),
        )
        assert isinstance(result, ExecutionRefusal)
        assert result.code is ExecutionRefusalCode.INSUFFICIENT_MARGIN

    def test_leverage_brings_margin_within_balance(self) -> None:
        """Тот же расчёт, но с плечом 100x — margin делится на leverage и
        укладывается в баланс."""
        result = calculate_size(
            account_balance=D("100"),
            risk_percent=D("50"),
            entry_price=D("100"),
            stop_loss=D("99"),
            side=TradeSide.LONG,
            leverage=100,
            symbol_info=_symbol_info(quantity_precision=0, min_quantity=D("1")),
        )
        assert isinstance(result, SizingResult)
        assert result.margin == D("50.00000000")


class TestZeroStopDistance:
    def test_entry_equals_stop_refused_not_raised(self) -> None:
        result = calculate_size(
            account_balance=D("1000"),
            risk_percent=D("1"),
            entry_price=D("100"),
            stop_loss=D("100"),
            side=TradeSide.LONG,
            leverage=1,
            symbol_info=_symbol_info(),
        )
        assert isinstance(result, ExecutionRefusal)
        assert result.code is ExecutionRefusalCode.INVALID_LEVELS

    def test_stop_on_wrong_side_refused_not_raised(self) -> None:
        """Стоп выше входа для LONG — тоже «дистанция ≤ 0» с точки зрения
        stop_distance(), а не отдельный код: sizing не размножает коды сверх
        раздела 7 ТЗ."""
        result = calculate_size(
            account_balance=D("1000"),
            risk_percent=D("1"),
            entry_price=D("100"),
            stop_loss=D("105"),
            side=TradeSide.LONG,
            leverage=1,
            symbol_info=_symbol_info(),
        )
        assert isinstance(result, ExecutionRefusal)
        assert result.code is ExecutionRefusalCode.INVALID_LEVELS



class TestRoundLevelsTowardEntry:
    """Шаг 15.5.3: уровни — к шагу цены символа, к цене входа (раздел 6 ТЗ:
    риск не больше заявленного, цель не завышается)."""

    def test_long_stop_up_take_down(self) -> None:
        stop, take = round_levels_toward_entry(
            stop_loss=Decimal("97.1234"), take_profit=Decimal("110.0567"),
            side=TradeSide.LONG, price_precision=1,
        )
        assert (stop, take) == (Decimal("97.2"), Decimal("110.0"))

    def test_short_stop_down_take_up(self) -> None:
        stop, take = round_levels_toward_entry(
            stop_loss=Decimal("103.0789"), take_profit=Decimal("90.0123"),
            side=TradeSide.SHORT, price_precision=1,
        )
        assert (stop, take) == (Decimal("103.0"), Decimal("90.1"))

    def test_already_on_step_unchanged(self) -> None:
        stop, take = round_levels_toward_entry(
            stop_loss=Decimal("97.5"), take_profit=Decimal("110.5"),
            side=TradeSide.LONG, price_precision=1,
        )
        assert (stop, take) == (Decimal("97.5"), Decimal("110.5"))
