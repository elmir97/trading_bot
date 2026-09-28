"""Taker-комиссия в RR гварда и в объёме (28.09, блок A).

Живые числа демо: #40 SOL (цена «Да» 122.943, стоп 121.646, тейк 126.077,
риск 1766.609542) и #37 LINK (14.398 / 13.526 / 16.263, риск 1777.022938),
комиссия 0.05% на ногу."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.exchanges.base import SymbolInfo
from app.execution.guards import check_valid_levels
from app.execution.models import ExecutionRefusalCode
from app.execution.sizing import SizingResult, calculate_size
from app.trading.calculations import calculate_risk_reward, calculate_risk_reward_net
from app.trading.enums import TradeSide

D = Decimal
FEE = D("0.0005")


class TestRiskRewardNet:
    def test_sol_40(self) -> None:
        assert calculate_risk_reward_net(
            entry_price=D("122.943"), stop_loss=D("121.646"), take_profit=D("126.077"),
            side=TradeSide.LONG, fee_rate=FEE,
        ) == D("2.12")

    def test_link_37(self) -> None:
        assert calculate_risk_reward_net(
            entry_price=D("14.398"), stop_loss=D("13.526"), take_profit=D("16.263"),
            side=TradeSide.LONG, fee_rate=FEE,
        ) == D("2.09")

    def test_short_symmetric(self) -> None:
        """SHORT: прибыль — вниз до тейка, комиссия съедает её так же."""
        assert calculate_risk_reward_net(
            entry_price=D("100"), stop_loss=D("102"), take_profit=D("96"),
            side=TradeSide.SHORT, fee_rate=FEE,
        ) == D("1.86")  # (4 − 0.0005·196) / (2 + 0.0005·202) = 3.902 / 2.101

    def test_zero_fee_equals_gross(self) -> None:
        kw = dict(entry_price=D("100"), stop_loss=D("97"), take_profit=D("106"),
                  side=TradeSide.LONG)
        assert calculate_risk_reward_net(**kw, fee_rate=D("0")) == calculate_risk_reward(**kw)


class TestValidLevelsFee:
    """Гвард INVALID_LEVELS сравнивает exec_min_rr с RR с комиссией."""

    def test_gross_passes_net_fails(self) -> None:
        refusal = check_valid_levels(
            entry_price=D("100"), stop_loss=D("97.9"), take_profit=D("103.2"),
            side=TradeSide.LONG, min_risk_reward=D("1.5"), fee_rate=FEE,
        )
        assert refusal is not None
        assert refusal.code is ExecutionRefusalCode.INVALID_LEVELS
        assert "RR 1:1.52" in refusal.message and "с комиссией 1:1.41" in refusal.message

    def test_same_levels_without_fee_pass(self) -> None:
        assert check_valid_levels(
            entry_price=D("100"), stop_loss=D("97.9"), take_profit=D("103.2"),
            side=TradeSide.LONG, min_risk_reward=D("1.5"), fee_rate=D("0"),
        ) is None


class TestSizeWithFee:
    def test_sol_40_quantity_and_loss_at_stop(self) -> None:
        """Без комиссии объём 1362.07 и убыток на стопе с комиссиями 1933.18
        при риске 1766.61; с комиссией — 1244.70 и ≤ риска."""
        info = SymbolInfo(
            symbol="SOL-USDT", price_precision=3, quantity_precision=2,
            min_quantity=D("0.01"), min_notional=D("5"),
        )
        result = calculate_size(
            account_balance=D("88330.4771"), risk_percent=D("2"),
            entry_price=D("122.943"), stop_loss=D("121.646"), side=TradeSide.LONG,
            leverage=10, symbol_info=info, fee_rate=FEE,
        )
        assert isinstance(result, SizingResult)
        assert result.quantity == D("1244.70")
        loss = result.quantity * (D("122.943") - D("121.646")) + FEE * result.quantity * (
            D("122.943") + D("121.646")
        )
        assert loss <= result.risk_amount


class TestFeeRateSetting:
    def test_default(self) -> None:
        assert Settings().exec_taker_fee_rate == D("0.0005")  # type: ignore[call-arg]

    @pytest.mark.parametrize("bad", ["0.05", "-0.0001", "0.01"])
    def test_out_of_range_refused(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            Settings(exec_taker_fee_rate=D(bad))  # type: ignore[call-arg]
