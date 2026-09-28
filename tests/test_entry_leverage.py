"""Плечо входа от стопа (28.09, блок D): min(plan.max_leverage,
floor(1 / (стоп × EXEC_LIQ_BUFFER + EXEC_MAINT_MARGIN_RATE))), не ниже 1.

Числа — живые: #110 XRP (стоп 13.84%), вход #37 LINK (14.398 / 13.526),
вход #40 SOL (122.943 / 121.646). Калибровка поддерживающей маржи — по
позиции LINK: 10x изолированная, ликвидация 13.08 при входе 14.4."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.execution.leverage import entry_leverage

D = Decimal
BUF, MMR = D("1.5"), D("0.008")


def _lev(entry: str, stop: str, max_leverage: int = 10) -> int:
    return entry_leverage(
        entry_price=D(entry), stop_loss=D(stop), max_leverage=max_leverage,
        liq_buffer=BUF, maint_margin_rate=MMR,
    )


def test_far_stop_xrp_110_gets_4x() -> None:
    assert _lev("100", "86.16") == 4  # 1 / (0.1384 × 1.5 + 0.008) = 4.64


def test_link_37_stays_at_plan_cap() -> None:
    assert _lev("14.398", "13.526") == 10  # 10.11 → потолок плана 10


def test_sol_40_stays_at_plan_cap() -> None:
    assert _lev("122.943", "121.646") == 10  # 42.1 → потолок плана 10


def test_short_side_symmetric() -> None:
    assert _lev("100", "113.84") == 4


def test_huge_stop_floors_at_one() -> None:
    assert _lev("100", "10") == 1


def test_plan_cap_is_respected() -> None:
    assert _lev("100", "99", max_leverage=3) == 3


def test_liquidation_with_buffer_is_beyond_stop() -> None:
    """Смысл формулы: при выбранном плече ликвидация (≈ 1/L − mmr) дальше
    стопа не меньше чем в EXEC_LIQ_BUFFER раз."""
    for entry, stop in (("100", "86.16"), ("14.398", "13.526"), ("100", "95")):
        leverage = _lev(entry, stop)
        stop_fraction = abs(D(entry) - D(stop)) / D(entry)
        assert D(1) / leverage - MMR >= stop_fraction * BUF


def test_settings_defaults_and_validation() -> None:
    settings = Settings()  # type: ignore[call-arg]
    assert (settings.exec_liq_buffer, settings.exec_maint_margin_rate) == (D("1.5"), D("0.008"))
    with pytest.raises(ValidationError):
        Settings(exec_liq_buffer=D("0.9"))  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(exec_maint_margin_rate=D("0.8"))  # type: ignore[call-arg]
