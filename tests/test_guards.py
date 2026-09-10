"""Тесты app/execution/guards.py (этап 15.3, раздел 7 и раздел 13 ТЗ).

Чистые функции на Decimal/datetime — без БД и без сети, поэтому без async
и без DATABASE_URL.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.exchanges.base import SymbolInfo
from app.execution.guards import (
    GuardInputs,
    check_daily_loss_limit,
    check_execution_enabled,
    check_max_positions,
    check_max_total_risk,
    check_mode_allowed,
    check_no_existing_position,
    check_price_drift,
    check_signal_not_expired,
    check_signal_not_used,
    check_size,
    check_symbol_allowed,
    check_trading_key,
    check_valid_levels,
    run_guards,
)
from app.execution.models import ExecutionRefusal
from app.execution.models import ExecutionRefusalCode as Code
from app.execution.sizing import SizingResult, calculate_size
from app.trading.enums import ExchangeKeyMode, TradeSide

D = Decimal
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _symbol_info(**overrides: object) -> SymbolInfo:
    fields: dict[str, object] = {
        "symbol": "BTC-USDT",
        "price_precision": 1,
        "quantity_precision": 3,
        "min_quantity": D("0.001"),
        "max_leverage": 50,
        "min_notional": D("5"),
    }
    fields.update(overrides)
    return SymbolInfo(**fields)  # type: ignore[arg-type]


def _valid_inputs(**overrides: object) -> GuardInputs:
    """Снимок, на котором ни один из 12 guard-ов не срабатывает."""
    fields: dict[str, object] = {
        "execution_enabled": True,
        "has_trading_key": True,
        "key_can_trade_futures": True,
        "selected_exchange_mode": ExchangeKeyMode.LIVE,
        "allowed_exchange_mode": ExchangeKeyMode.LIVE,
        "signal_expires_at": NOW + timedelta(hours=1),
        "now": NOW,
        "signal_trade_opened_at": None,
        "has_open_position": False,
        "open_positions_count": 1,
        "max_positions": 4,
        "current_total_risk_percent": D("1"),
        "new_risk_percent": D("1"),
        "max_total_risk_percent": D("5"),
        "day_loss_percent": D("1"),
        "max_daily_loss_percent": D("6"),
        "planned_price": D("100"),
        "current_price": D("100.1"),
        "max_price_drift_ratio": D("0.3"),
        "entry_price": D("100.1"),
        "stop_loss": D("97"),
        "take_profit": D("106"),
        "side": TradeSide.LONG,
        "min_risk_reward": D("1.5"),
        "account_balance": D("1000"),
        "leverage": 5,
        "symbol_info": _symbol_info(),
        "symbol": "BTC-USDT",
        "symbol_whitelist": ["BTC-USDT", "ETH-USDT"],
    }
    fields.update(overrides)
    return GuardInputs(**fields)  # type: ignore[arg-type]


# --- Каждый guard отдельно ---------------------------------------------------


class TestExecutionEnabled:
    def test_disabled_refuses(self) -> None:
        refusal = check_execution_enabled(execution_enabled=False)
        assert refusal is not None
        assert refusal.code is Code.EXECUTION_DISABLED

    def test_enabled_passes(self) -> None:
        assert check_execution_enabled(execution_enabled=True) is None


class TestTradingKey:
    def test_no_key_refuses(self) -> None:
        refusal = check_trading_key(has_key=False, key_can_trade_futures=False)
        assert refusal is not None
        assert refusal.code is Code.NO_TRADING_KEY

    def test_key_without_futures_permission_refuses(self) -> None:
        refusal = check_trading_key(has_key=True, key_can_trade_futures=False)
        assert refusal is not None
        assert refusal.code is Code.NO_TRADING_KEY

    def test_valid_key_passes(self) -> None:
        assert check_trading_key(has_key=True, key_can_trade_futures=True) is None


class TestModeNotAllowed:
    """Этап 15.4в: показанный в настройках счёт против разрешённого конфигом."""

    def test_live_selected_but_demo_allowed_refuses(self) -> None:
        refusal = check_mode_allowed(
            selected_mode=ExchangeKeyMode.LIVE, allowed_mode=ExchangeKeyMode.DEMO
        )
        assert refusal is not None
        assert refusal.code is Code.MODE_NOT_ALLOWED
        assert "демо-счёте" in refusal.message

    def test_demo_selected_but_live_allowed_refuses(self) -> None:
        """Симметрично: рассинхрон блокирует вход в обе стороны, не только
        когда пользователь «смотрит выше», чем разрешено."""
        refusal = check_mode_allowed(
            selected_mode=ExchangeKeyMode.DEMO, allowed_mode=ExchangeKeyMode.LIVE
        )
        assert refusal is not None
        assert refusal.code is Code.MODE_NOT_ALLOWED
        assert "реальном счёте" in refusal.message

    def test_matching_live_passes(self) -> None:
        assert check_mode_allowed(
            selected_mode=ExchangeKeyMode.LIVE, allowed_mode=ExchangeKeyMode.LIVE
        ) is None

    def test_matching_demo_passes(self) -> None:
        assert check_mode_allowed(
            selected_mode=ExchangeKeyMode.DEMO, allowed_mode=ExchangeKeyMode.DEMO
        ) is None


class TestSignalExpired:
    def test_expired_refuses(self) -> None:
        refusal = check_signal_not_expired(
            expires_at=NOW - timedelta(seconds=1), now=NOW
        )
        assert refusal is not None
        assert refusal.code is Code.SIGNAL_EXPIRED

    def test_exactly_at_expiry_refuses(self) -> None:
        """now == expires_at — TTL истёк, а не «ещё секунда в запасе»."""
        refusal = check_signal_not_expired(expires_at=NOW, now=NOW)
        assert refusal is not None
        assert refusal.code is Code.SIGNAL_EXPIRED

    def test_not_expired_passes(self) -> None:
        assert check_signal_not_expired(expires_at=NOW + timedelta(seconds=1), now=NOW) is None


class TestSignalAlreadyUsed:
    def test_used_refuses(self) -> None:
        refusal = check_signal_not_used(trade_opened_at=NOW)
        assert refusal is not None
        assert refusal.code is Code.SIGNAL_ALREADY_USED

    def test_unused_passes(self) -> None:
        assert check_signal_not_used(trade_opened_at=None) is None


class TestPositionExists:
    def test_existing_position_refuses(self) -> None:
        refusal = check_no_existing_position(has_open_position=True)
        assert refusal is not None
        assert refusal.code is Code.POSITION_EXISTS

    def test_no_position_passes(self) -> None:
        assert check_no_existing_position(has_open_position=False) is None


class TestMaxPositions:
    def test_at_limit_refuses(self) -> None:
        refusal = check_max_positions(open_positions_count=4, max_positions=4)
        assert refusal is not None
        assert refusal.code is Code.MAX_POSITIONS

    def test_over_limit_refuses(self) -> None:
        refusal = check_max_positions(open_positions_count=5, max_positions=4)
        assert refusal is not None
        assert refusal.code is Code.MAX_POSITIONS

    def test_below_limit_passes(self) -> None:
        assert check_max_positions(open_positions_count=3, max_positions=4) is None


class TestMaxTotalRisk:
    def test_exceeding_refuses(self) -> None:
        refusal = check_max_total_risk(
            current_total_risk_percent=D("4"),
            new_risk_percent=D("2"),
            max_total_risk_percent=D("5"),
        )
        assert refusal is not None
        assert refusal.code is Code.MAX_TOTAL_RISK

    def test_exactly_at_limit_passes(self) -> None:
        """Ровно лимит — ещё не превышение (проверка строго '>')."""
        refusal = check_max_total_risk(
            current_total_risk_percent=D("3"),
            new_risk_percent=D("2"),
            max_total_risk_percent=D("5"),
        )
        assert refusal is None

    def test_within_limit_passes(self) -> None:
        assert check_max_total_risk(
            current_total_risk_percent=D("1"),
            new_risk_percent=D("1"),
            max_total_risk_percent=D("5"),
        ) is None


class TestDailyLossLimit:
    def test_limit_reached_refuses(self) -> None:
        refusal = check_daily_loss_limit(
            day_loss_percent=D("6"), max_daily_loss_percent=D("6")
        )
        assert refusal is not None
        assert refusal.code is Code.DAILY_LOSS_LIMIT

    def test_no_data_passes(self) -> None:
        """day_loss_percent=None — сделок сегодня ещё не было, PnL считать не от чего."""
        assert check_daily_loss_limit(day_loss_percent=None, max_daily_loss_percent=D("6")) is None

    def test_below_limit_passes(self) -> None:
        assert check_daily_loss_limit(
            day_loss_percent=D("3"), max_daily_loss_percent=D("6")
        ) is None


class TestPriceDrift:
    def test_drift_beyond_threshold_refuses(self) -> None:
        refusal = check_price_drift(
            planned_price=D("100"), current_price=D("101"),
            stop_loss=D("99"), max_drift_ratio=D("0.3"),
        )
        # дистанция до стопа = 1, допустимый дрейф = 0.3, факт = 1 > 0.3
        assert refusal is not None
        assert refusal.code is Code.PRICE_DRIFT

    def test_drift_within_threshold_passes(self) -> None:
        assert check_price_drift(
            planned_price=D("100"), current_price=D("100.2"),
            stop_loss=D("99"), max_drift_ratio=D("0.3"),
        ) is None

    def test_drift_direction_does_not_matter(self) -> None:
        refusal = check_price_drift(
            planned_price=D("100"), current_price=D("99"),
            stop_loss=D("99"), max_drift_ratio=D("0.3"),
        )
        assert refusal is not None
        assert refusal.code is Code.PRICE_DRIFT


class TestValidLevels:
    def test_stop_on_wrong_side_refuses(self) -> None:
        refusal = check_valid_levels(
            entry_price=D("100"), stop_loss=D("105"), take_profit=D("110"),
            side=TradeSide.LONG, min_risk_reward=D("1.5"),
        )
        assert refusal is not None
        assert refusal.code is Code.INVALID_LEVELS

    def test_rr_below_minimum_refuses(self) -> None:
        refusal = check_valid_levels(
            entry_price=D("100"), stop_loss=D("99"), take_profit=D("101"),
            side=TradeSide.LONG, min_risk_reward=D("1.5"),
        )
        assert refusal is not None
        assert refusal.code is Code.INVALID_LEVELS

    def test_valid_levels_pass(self) -> None:
        assert check_valid_levels(
            entry_price=D("100"), stop_loss=D("97"), take_profit=D("106"),
            side=TradeSide.LONG, min_risk_reward=D("1.5"),
        ) is None


class TestSize:
    def test_forwards_sizing_refusal(self) -> None:
        sizing = calculate_size(
            account_balance=D("10"),
            risk_percent=D("1"),
            entry_price=D("100"),
            stop_loss=D("97"),
            side=TradeSide.LONG,
            leverage=1,
            symbol_info=_symbol_info(min_quantity=D("100")),
        )
        assert isinstance(sizing, ExecutionRefusal)
        assert check_size(sizing=sizing) is sizing

    def test_successful_sizing_passes(self) -> None:
        sizing = SizingResult(
            quantity=D("1"), notional=D("100"), margin=D("20"), risk_amount=D("10")
        )
        assert check_size(sizing=sizing) is None


class TestSymbolAllowed:
    def test_outside_whitelist_refuses(self) -> None:
        refusal = check_symbol_allowed(symbol="XRP-USDT", whitelist=["BTC-USDT"])
        assert refusal is not None
        assert refusal.code is Code.SYMBOL_NOT_ALLOWED

    def test_inside_whitelist_passes(self) -> None:
        assert check_symbol_allowed(symbol="BTC-USDT", whitelist=["BTC-USDT"]) is None

    def test_case_insensitive(self) -> None:
        assert check_symbol_allowed(symbol="btc-usdt", whitelist=["BTC-USDT"]) is None

    def test_empty_whitelist_allows_everything(self) -> None:
        assert check_symbol_allowed(symbol="ANY-USDT", whitelist=[]) is None


# --- Порядок срабатывания ----------------------------------------------------

# (guard_number, expected_code, overrides для отказа именно этого guard-а),
# в порядке раздела 7 ТЗ.
GUARD_ORDER: list[tuple[int, Code, dict[str, object]]] = [
    (1, Code.EXECUTION_DISABLED, {"execution_enabled": False}),
    (2, Code.NO_TRADING_KEY, {"has_trading_key": False}),
    (3, Code.MODE_NOT_ALLOWED, {"selected_exchange_mode": ExchangeKeyMode.DEMO}),
    (4, Code.SIGNAL_EXPIRED, {"signal_expires_at": NOW - timedelta(seconds=1)}),
    (5, Code.SIGNAL_ALREADY_USED, {"signal_trade_opened_at": NOW}),
    (6, Code.POSITION_EXISTS, {"has_open_position": True}),
    (7, Code.MAX_POSITIONS, {"open_positions_count": 4, "max_positions": 4}),
    (
        8,
        Code.MAX_TOTAL_RISK,
        {"current_total_risk_percent": D("10"), "max_total_risk_percent": D("1")},
    ),
    (
        9,
        Code.DAILY_LOSS_LIMIT,
        {"day_loss_percent": D("10"), "max_daily_loss_percent": D("1")},
    ),
    (10, Code.PRICE_DRIFT, {"current_price": D("10000")}),
    (11, Code.INVALID_LEVELS, {"stop_loss": D("105")}),
    (12, Code.SIZE_TOO_SMALL, {"symbol_info": _symbol_info(min_quantity=D("1000"))}),
    (13, Code.SYMBOL_NOT_ALLOWED, {"symbol": "XRP-USDT"}),
]


class TestGuardOrder:
    def test_all_valid_passes(self) -> None:
        assert run_guards(_valid_inputs()) is None

    def test_first_guard_wins_when_everything_fails(self) -> None:
        """Каждое условие отказа выставлено разом — обязан сработать guard
        раздела 7 п.1, а не любой другой."""
        inputs = _valid_inputs(
            execution_enabled=False,
            has_trading_key=False,
            key_can_trade_futures=False,
            selected_exchange_mode=ExchangeKeyMode.DEMO,
            signal_expires_at=NOW - timedelta(seconds=1),
            signal_trade_opened_at=NOW,
            has_open_position=True,
            open_positions_count=10,
            max_positions=1,
            current_total_risk_percent=D("10"),
            new_risk_percent=D("10"),
            max_total_risk_percent=D("1"),
            day_loss_percent=D("10"),
            max_daily_loss_percent=D("1"),
            current_price=D("10000"),
            stop_loss=D("105"),  # неверная сторона для LONG
            symbol="XRP-USDT",
            symbol_whitelist=["BTC-USDT"],
        )
        refusal = run_guards(inputs)
        assert refusal is not None
        assert refusal.code is Code.EXECUTION_DISABLED

    @pytest.mark.parametrize("guard_number,expected_code,overrides", GUARD_ORDER)
    def test_guard_fires_in_isolation(
        self, guard_number: int, expected_code: Code, overrides: dict[str, object]
    ) -> None:
        refusal = run_guards(_valid_inputs(**overrides))
        assert refusal is not None
        assert refusal.code is expected_code, (
            f"guard #{guard_number} ({expected_code}) didn't fire, "
            f"got {refusal.code} instead"
        )

    def test_order_is_respected_for_every_adjacent_pair(self) -> None:
        """Совмещаем отказ guard-а N с отказом guard-а N+1 — обязан
        сработать N, а не N+1: раздел 7 требует именно этот порядок."""
        for i in range(len(GUARD_ORDER) - 1):
            _, code_earlier, overrides_earlier = GUARD_ORDER[i]
            _, _, overrides_later = GUARD_ORDER[i + 1]
            combined = {**overrides_earlier, **overrides_later}
            refusal = run_guards(_valid_inputs(**combined))
            assert refusal is not None
            assert refusal.code is code_earlier, (
                f"pair {i+1}&{i+2}: expected {code_earlier}, got {refusal.code}"
            )
