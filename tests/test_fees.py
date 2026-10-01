"""Taker-комиссия в RR (28.09, блок A) и настройка ставки.

Живые числа демо: #40 SOL (цена «Да» 122.943, стоп 121.646, тейк 126.077,
риск 1766.609542) и #37 LINK (14.398 / 13.526 / 16.263, риск 1777.022938),
комиссия 0.05% на ногу."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.config import Settings
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
        kw = {
            "entry_price": D("100"), "stop_loss": D("97"), "take_profit": D("106"),
            "side": TradeSide.LONG,
        }
        assert calculate_risk_reward_net(**kw, fee_rate=D("0")) == calculate_risk_reward(**kw)


class TestFeeRateSetting:
    def test_default(self) -> None:
        assert Settings().exec_taker_fee_rate == D("0.0005")  # type: ignore[call-arg]

    @pytest.mark.parametrize("bad", ["0.05", "-0.0001", "0.01"])
    def test_out_of_range_refused(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            Settings(exec_taker_fee_rate=D(bad))  # type: ignore[call-arg]
