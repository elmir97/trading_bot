"""Тесты app/execution/guards.py — общие проверки исполнения (этап 15.3,
раздел 7 ТЗ). Проверки входа по сигналу удалены вместе с ним (02.10.2026).

Чистые функции — без БД и без сети.
"""

from __future__ import annotations

from app.execution.guards import (
    check_execution_enabled,
    check_live_orders_allowed,
    check_mode_allowed,
    check_permissions_trustworthy,
    check_position_mode_known,
    check_trading_key,
)
from app.execution.models import ExecutionRefusalCode as Code
from app.trading.enums import ExchangeKeyMode


class TestExecutionEnabled:
    def test_disabled_refuses(self) -> None:
        refusal = check_execution_enabled(execution_enabled=False)
        assert refusal is not None
        assert refusal.code is Code.EXECUTION_DISABLED

    def test_enabled_passes(self) -> None:
        assert check_execution_enabled(execution_enabled=True) is None


class TestLiveOrdersAllowed:
    """Раздел 16 ТЗ, шаг 15.5.1."""

    def test_live_without_flag_refuses(self) -> None:
        refusal = check_live_orders_allowed(
            trading_mode="live", allow_live_mode_orders=False
        )
        assert refusal is not None
        assert refusal.code is Code.LIVE_ORDERS_NOT_ALLOWED

    def test_live_with_flag_passes(self) -> None:
        assert (
            check_live_orders_allowed(trading_mode="live", allow_live_mode_orders=True)
            is None
        )

    def test_demo_without_flag_passes(self) -> None:
        """Флаг вообще не при чём на demo — ограничение только про LIVE."""
        assert (
            check_live_orders_allowed(trading_mode="demo", allow_live_mode_orders=False)
            is None
        )


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


class TestPermissionsTrustworthy:
    def test_untrustworthy_refuses(self) -> None:
        refusal = check_permissions_trustworthy(trustworthy=False)
        assert refusal is not None
        assert refusal.code is Code.PERMISSIONS_UNKNOWN

    def test_trustworthy_passes(self) -> None:
        assert check_permissions_trustworthy(trustworthy=True) is None


class TestPositionModeKnown:
    """Раздел 16 ТЗ, шаг 15.5.1 — по образцу TestPermissionsTrustworthy
    выше: тот же принцип (сбой не значит "можно"), другой источник."""

    def test_unknown_refuses(self) -> None:
        refusal = check_position_mode_known(known=False)
        assert refusal is not None
        assert refusal.code is Code.POSITION_MODE_UNKNOWN

    def test_known_passes(self) -> None:
        assert check_position_mode_known(known=True) is None
