"""Тесты проверки сделки на соответствие торговому плану."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio

from app.core.config import Settings
from app.database.models.trading_plan import TradingPlan
from app.database.repositories.strategy import (
    MistakeTypeRepository,
    StrategyRepository,
)
from app.database.repositories.trade import TradeRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.services.user_service import UserService
from app.trading.enums import TradeSide
from app.trading.journal import TradeJournal
from app.trading.risk import PlanValidator, ViolationCode, day_bounds, week_bounds

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL"
)

D = Decimal


def make_plan() -> TradingPlan:
    return TradingPlan(
        user_id=0,
        risk_per_trade_percent=D("2"),
        max_daily_loss_percent=D("6"),
        max_weekly_loss_percent=D("10"),
        max_trades_per_day=5,
        min_risk_reward=D("2"),
        max_leverage=10,
        allowed_symbols=["BTC-USDT", "ETH-USDT"],
        allowed_timeframes=["1h", "4h"],
    )


@pytest_asyncio.fixture
async def validator():  # type: ignore[no-untyped-def]
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    async with db.session() as session:
        svc = UserService(
            UserRepository(session),
            StrategyRepository(session),
            MistakeTypeRepository(session),
            settings,
        )
        tg = 800_000 + int(datetime.now(UTC).timestamp() * 1000) % 90_000
        user = await svc.get_or_create(telegram_id=tg)
        repo = TradeRepository(session)
        yield PlanValidator(repo), user, repo
    await db.dispose()


async def _check(validator, **overrides):  # type: ignore[no-untyped-def]
    v, user, _repo = validator
    params = {
        "plan": make_plan(),
        "user_id": user.id,
        "symbol": "BTC-USDT",
        "side": TradeSide.LONG,
        "entry_price": D("100000"),
        "quantity": D("0.1"),
        "stop_loss": D("98000"),
        "take_profit": D("104000"),
        "leverage": 10,
        "timeframe": "1h",
        "account_balance": D("10000"),
    }
    params.update(overrides)
    return await v.check(**params)


async def test_compliant_trade_passes(validator) -> None:  # type: ignore[no-untyped-def]
    result = await _check(validator)
    assert result.ok, result.render()


async def test_missing_stop_is_flagged(validator) -> None:  # type: ignore[no-untyped-def]
    result = await _check(validator, stop_loss=None, take_profit=None)
    codes = {v.code for v in result.violations}
    assert ViolationCode.NO_STOP_LOSS in codes


async def test_excessive_risk_flagged(validator) -> None:  # type: ignore[no-untyped-def]
    """Объём 0.3 при стопе 2000 пунктов = риск 600 при балансе 10000 = 6%."""
    result = await _check(validator, quantity=D("0.3"))
    codes = {v.code for v in result.violations}
    assert ViolationCode.RISK_TOO_HIGH in codes
    message = next(v.message for v in result.violations if v.code is ViolationCode.RISK_TOO_HIGH)
    assert "6.00%" in message
    assert "2%" in message


async def test_low_rr_flagged(validator) -> None:  # type: ignore[no-untyped-def]
    result = await _check(validator, take_profit=D("101000"))
    codes = {v.code for v in result.violations}
    assert ViolationCode.LOW_RISK_REWARD in codes
    message = next(
        v.message for v in result.violations if v.code is ViolationCode.LOW_RISK_REWARD
    )
    assert "RR 1:0.5" in message
    assert "1:2" in message


async def test_symbol_outside_plan_flagged(validator) -> None:  # type: ignore[no-untyped-def]
    result = await _check(validator, symbol="PEPE-USDT")
    codes = {v.code for v in result.violations}
    assert ViolationCode.SYMBOL_NOT_ALLOWED in codes
    message = next(
        v.message for v in result.violations if v.code is ViolationCode.SYMBOL_NOT_ALLOWED
    )
    assert "PEPE-USDT" in message
    assert "BTC-USDT" in message
    assert "ETH-USDT" in message


async def test_timeframe_outside_plan_flagged(validator) -> None:  # type: ignore[no-untyped-def]
    result = await _check(validator, timeframe="5m")
    codes = {v.code for v in result.violations}
    assert ViolationCode.TIMEFRAME_NOT_ALLOWED in codes
    message = next(
        v.message for v in result.violations if v.code is ViolationCode.TIMEFRAME_NOT_ALLOWED
    )
    assert "5m" in message
    assert "1h" in message
    assert "4h" in message


async def test_excessive_leverage_flagged(validator) -> None:  # type: ignore[no-untyped-def]
    result = await _check(validator, leverage=50)
    codes = {v.code for v in result.violations}
    assert ViolationCode.LEVERAGE_TOO_HIGH in codes
    message = next(
        v.message for v in result.violations if v.code is ViolationCode.LEVERAGE_TOO_HIGH
    )
    assert "50x" in message
    assert "10x" in message


async def test_daily_trade_limit_message_has_counter_and_cap(validator) -> None:  # type: ignore[no-untyped-def]
    """max_trades_per_day=5 в make_plan(): 5 сделок, открытых сегодня, достигают лимита."""
    _v, user, repo = validator
    journal = TradeJournal(repo)
    for _ in range(5):
        await journal.open_trade(
            user_id=user.id,
            symbol="BTC-USDT",
            side=TradeSide.LONG,
            entry_price=D("100000"),
            quantity=D("0.01"),
        )

    result = await _check(validator)
    message = next(
        v.message for v in result.violations if v.code is ViolationCode.DAILY_TRADE_LIMIT
    )
    assert "5 сделок" in message
    assert "лимите 5" in message


async def test_violations_are_not_blocking_by_default(validator) -> None:  # type: ignore[no-untyped-def]
    """Нарушения предупреждают, но не запрещают запись сделки."""
    result = await _check(validator, stop_loss=None, take_profit=None)
    assert not result.ok
    assert not result.has_blocking


class TestPeriodBounds:
    def test_day_starts_at_local_midnight(self) -> None:
        """Для UTC+5 день начинается в 19:00 UTC предыдущих суток."""
        now = datetime(2026, 3, 15, 10, 0, tzinfo=UTC)
        start, end = day_bounds(now, tz_offset_hours=5)
        assert start == datetime(2026, 3, 14, 19, 0, tzinfo=UTC)
        assert (end - start).days == 1

    def test_evening_trade_belongs_to_same_local_day(self) -> None:
        """Сделка в 23:00 по Перми (18:00 UTC) — ещё сегодняшний день."""
        now = datetime(2026, 3, 15, 18, 0, tzinfo=UTC)
        start, end = day_bounds(now, tz_offset_hours=5)
        assert start <= now < end

    def test_week_starts_on_monday(self) -> None:
        # 18 марта 2026 — среда
        now = datetime(2026, 3, 18, 12, 0, tzinfo=UTC)
        start, end = week_bounds(now, tz_offset_hours=5)
        assert (end - start).days == 7
        assert start <= now < end
