"""Интеграционные тесты журнала сделок.

Проверяем полный цикл: открытие, частичное закрытие, полное закрытие,
пересчёт агрегатов из fills. Против настоящей базы, а не моков.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from app.core.config import Settings
from app.database.repositories.strategy import (
    MistakeTypeRepository,
    StrategyRepository,
)
from app.database.repositories.trade import TradeRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.services.user_service import UserService
from app.trading.enums import FillSide, TradeSide, TradeStatus
from app.trading.journal import JournalError, TradeJournal

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL"
)

D = Decimal


@pytest_asyncio.fixture
async def ctx():  # type: ignore[no-untyped-def]
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    async with db.session() as session:
        user_service = UserService(
            UserRepository(session),
            StrategyRepository(session),
            MistakeTypeRepository(session),
            settings,
        )
        # Уникальный id на каждый тест: изоляция без очистки таблиц.
        telegram_id = 900_000 + int(datetime.now(UTC).timestamp() * 1000) % 90_000
        user = await user_service.get_or_create(telegram_id=telegram_id)
        repo = TradeRepository(session)
        yield user, TradeJournal(repo), repo, session
    await db.dispose()


async def test_open_trade_creates_entry_fill(ctx) -> None:  # type: ignore[no-untyped-def]
    user, journal, repo, _ = ctx
    trade = await journal.open_trade(
        user_id=user.id, symbol="BTC-USDT", side=TradeSide.LONG,
        entry_price=D("100000"), quantity=D("0.1"),
        stop_loss=D("98000"), take_profit=D("104000"),
        leverage=10, account_balance=D("10000"),
    )

    assert trade.status is TradeStatus.OPEN
    assert trade.entry_price == D("100000")
    assert len(trade.fills) == 1
    assert trade.fills[0].fill_side is FillSide.ENTRY
    assert trade.risk_reward == D("2.00")
    # Риск: (100000-98000) * 0.1 = 200 при балансе 10000 → 2%
    assert trade.risk_percent == D("2.0000")


async def test_rejects_stop_on_wrong_side(ctx) -> None:  # type: ignore[no-untyped-def]
    user, journal, _, _ = ctx
    from app.trading.calculations import CalculationError

    with pytest.raises(CalculationError):
        await journal.open_trade(
            user_id=user.id, symbol="ETH-USDT", side=TradeSide.LONG,
            entry_price=D("3000"), quantity=D("1"), stop_loss=D("3100"),
        )


async def test_full_close_computes_pnl(ctx) -> None:  # type: ignore[no-untyped-def]
    user, journal, _, _ = ctx
    trade = await journal.open_trade(
        user_id=user.id, symbol="ETH-USDT", side=TradeSide.LONG,
        entry_price=D("3000"), quantity=D("1"), stop_loss=D("2940"),
        fee=D("1.5"), account_balance=D("10000"),
    )
    await journal.close_trade(trade, exit_price=D("3120"), fee=D("1.5"))

    assert trade.status is TradeStatus.CLOSED
    assert trade.closed_at is not None
    # (3120 - 3000) * 1 - 3.0 комиссий
    assert trade.pnl == D("117")
    assert trade.pnl_percent == D("1.1700")  # от баланса 10000
    # Стоп на 2940 = риск 60; результат 120 пунктов = +2R
    assert trade.risk_reward == D("2.00")


async def test_short_profit_on_drop(ctx) -> None:  # type: ignore[no-untyped-def]
    user, journal, _, _ = ctx
    trade = await journal.open_trade(
        user_id=user.id, symbol="SOL-USDT", side=TradeSide.SHORT,
        entry_price=D("200"), quantity=D("10"), stop_loss=D("206"),
    )
    await journal.close_trade(trade, exit_price=D("188"))
    assert trade.pnl == D("120")


async def test_partial_close_keeps_trade_open(ctx) -> None:  # type: ignore[no-untyped-def]
    user, journal, _, _ = ctx
    trade = await journal.open_trade(
        user_id=user.id, symbol="BTC-USDT", side=TradeSide.LONG,
        entry_price=D("100000"), quantity=D("0.2"), stop_loss=D("98000"),
    )
    await journal.close_trade(trade, exit_price=D("102000"), quantity=D("0.1"))

    assert trade.status is TradeStatus.OPEN
    # PnL начислен только на закрытую половину
    assert trade.pnl == D("200")

    await journal.close_trade(trade, exit_price=D("103000"), quantity=D("0.1"))
    assert trade.status is TradeStatus.CLOSED
    # Средняя цена выхода 102500, объём 0.2 → 500
    assert trade.exit_price == D("102500")
    assert trade.pnl == D("500")


async def test_cannot_close_more_than_open(ctx) -> None:  # type: ignore[no-untyped-def]
    user, journal, _, _ = ctx
    trade = await journal.open_trade(
        user_id=user.id, symbol="XRP-USDT", side=TradeSide.LONG,
        entry_price=D("2"), quantity=D("100"),
    )
    with pytest.raises(JournalError, match="Нельзя закрыть"):
        await journal.close_trade(trade, exit_price=D("2.1"), quantity=D("150"))


async def test_scaled_entry_averages_price(ctx) -> None:  # type: ignore[no-untyped-def]
    user, journal, _, _ = ctx
    trade = await journal.open_trade(
        user_id=user.id, symbol="ADA-USDT", side=TradeSide.LONG,
        entry_price=D("1.00"), quantity=D("100"),
    )
    await journal.add_fill(
        trade, fill_side=FillSide.ENTRY, price=D("1.10"), quantity=D("100")
    )
    assert trade.entry_price == D("1.05")
    assert trade.quantity == D("200")


async def test_double_close_rejected(ctx) -> None:  # type: ignore[no-untyped-def]
    user, journal, _, _ = ctx
    trade = await journal.open_trade(
        user_id=user.id, symbol="DOGE-USDT", side=TradeSide.LONG,
        entry_price=D("0.4"), quantity=D("1000"),
    )
    await journal.close_trade(trade, exit_price=D("0.42"))
    with pytest.raises(JournalError, match="уже закрыта"):
        await journal.close_trade(trade, exit_price=D("0.43"))


async def test_recalculate_is_idempotent(ctx) -> None:  # type: ignore[no-untyped-def]
    user, journal, _, _ = ctx
    trade = await journal.open_trade(
        user_id=user.id, symbol="LINK-USDT", side=TradeSide.LONG,
        entry_price=D("20"), quantity=D("50"), stop_loss=D("19"),
    )
    await journal.close_trade(trade, exit_price=D("22"))

    first = (trade.pnl, trade.pnl_percent, trade.risk_reward)
    journal.recalculate(trade)
    journal.recalculate(trade)
    assert (trade.pnl, trade.pnl_percent, trade.risk_reward) == first


async def test_repository_isolates_users(ctx) -> None:  # type: ignore[no-untyped-def]
    user, journal, repo, session = ctx
    trade = await journal.open_trade(
        user_id=user.id, symbol="TON-USDT", side=TradeSide.LONG,
        entry_price=D("5"), quantity=D("10"),
    )
    # Запрос от имени чужого пользователя не должен вернуть сделку.
    assert await repo.get(trade.id, user_id=user.id + 999_999) is None
    assert await repo.get(trade.id, user_id=user.id) is not None


async def test_open_positions_listed(ctx) -> None:  # type: ignore[no-untyped-def]
    user, journal, repo, _ = ctx
    await journal.open_trade(
        user_id=user.id, symbol="AVAX-USDT", side=TradeSide.LONG,
        entry_price=D("30"), quantity=D("10"),
    )
    closed = await journal.open_trade(
        user_id=user.id, symbol="BNB-USDT", side=TradeSide.LONG,
        entry_price=D("600"), quantity=D("1"),
    )
    await journal.close_trade(closed, exit_price=D("610"))

    open_trades = await repo.list_open(user.id)
    assert [t.symbol for t in open_trades] == ["AVAX-USDT"]


async def test_daily_trade_count(ctx) -> None:  # type: ignore[no-untyped-def]
    user, journal, repo, _ = ctx
    now = datetime.now(UTC)
    for _ in range(3):
        await journal.open_trade(
            user_id=user.id, symbol="BTC-USDT", side=TradeSide.LONG,
            entry_price=D("100000"), quantity=D("0.01"), opened_at=now,
        )
    count = await repo.count_opened_between(
        user.id, now - timedelta(hours=1), now + timedelta(hours=1)
    )
    assert count == 3
