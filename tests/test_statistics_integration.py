"""Сквозной тест: сделки через журнал → статистика из базы.

Проверяет стык, который юнит-тесты не покрывают: правильно ли SQL-проекция
превращается в снимки и совпадают ли цифры с ручным расчётом.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from app.core.config import Settings
from app.database.models.mistake import TradeMistake
from app.database.repositories.strategy import (
    MistakeTypeRepository,
    StrategyRepository,
)
from app.database.repositories.trade import TradeRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.services.statistics_service import StatisticsService, period_bounds
from app.services.user_service import UserService
from app.trading.enums import TradeSide
from app.trading.journal import TradeJournal
from app.trading.statistics import calculate_statistics, group_by
from tests.conftest import cleanup_user

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL"
)

D = Decimal


@pytest_asyncio.fixture
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    async with db.session() as session:
        svc = UserService(
            UserRepository(session),
            StrategyRepository(session),
            MistakeTypeRepository(session),
            settings,
        )
        user = await svc.get_or_create(telegram_id=unique_telegram_id())
        await svc.ensure_system_mistakes()
        repo = TradeRepository(session)
        yield user, TradeJournal(repo), session
        await cleanup_user(session, user)
    await db.dispose()


async def _closed_trade(  # type: ignore[no-untyped-def]
    journal, user_id, symbol, side, entry, exit_price, qty,
    strategy_id=None, when=None,
):
    moment = when or datetime.now(UTC)
    trade = await journal.open_trade(
        user_id=user_id, symbol=symbol, side=side,
        entry_price=D(entry), quantity=D(qty),
        strategy_id=strategy_id, opened_at=moment,
    )
    await journal.close_trade(
        trade, exit_price=D(exit_price), closed_at=moment + timedelta(minutes=30)
    )
    return trade


async def test_statistics_match_manual_calculation(ctx) -> None:  # type: ignore[no-untyped-def]
    user, journal, session = ctx
    base = datetime.now(UTC) - timedelta(hours=10)

    # +100, -50, +200 → win rate 66.67%, PnL +250
    await _closed_trade(journal, user.id, "BTC-USDT", TradeSide.LONG,
                        "1000", "1100", "1", when=base)
    await _closed_trade(journal, user.id, "ETH-USDT", TradeSide.LONG,
                        "1000", "950", "1", when=base + timedelta(hours=1))
    await _closed_trade(journal, user.id, "BTC-USDT", TradeSide.SHORT,
                        "1000", "800", "1", when=base + timedelta(hours=2))

    snapshots = await StatisticsService(session).load_snapshots(user.id)
    stats = calculate_statistics(snapshots)

    assert stats.total_trades == 3
    assert stats.wins == 2
    assert stats.total_pnl == D("250.00")
    assert stats.win_rate == D("66.67")
    assert stats.profit_factor == D("6.000")  # 300 / 50


async def test_symbol_breakdown_from_db(ctx) -> None:  # type: ignore[no-untyped-def]
    user, journal, session = ctx
    base = datetime.now(UTC) - timedelta(hours=5)

    await _closed_trade(journal, user.id, "BTC-USDT", TradeSide.LONG,
                        "100", "120", "1", when=base)
    await _closed_trade(journal, user.id, "BTC-USDT", TradeSide.LONG,
                        "100", "110", "1", when=base + timedelta(hours=1))
    await _closed_trade(journal, user.id, "SOL-USDT", TradeSide.LONG,
                        "100", "80", "1", when=base + timedelta(hours=2))

    snapshots = await StatisticsService(session).load_snapshots(user.id)
    groups = {g.key: g for g in group_by(snapshots, "symbol")}

    assert groups["BTC-USDT"].total_pnl == D("30.00")
    assert groups["SOL-USDT"].total_pnl == D("-20.00")
    assert groups["BTC-USDT"].win_rate == D("100.00")


async def test_side_breakdown(ctx) -> None:  # type: ignore[no-untyped-def]
    """Enum стороны должен корректно доезжать до среза строкой."""
    user, journal, session = ctx
    base = datetime.now(UTC) - timedelta(hours=4)

    await _closed_trade(journal, user.id, "BTC-USDT", TradeSide.LONG,
                        "100", "110", "1", when=base)
    await _closed_trade(journal, user.id, "BTC-USDT", TradeSide.SHORT,
                        "100", "110", "1", when=base + timedelta(hours=1))

    snapshots = await StatisticsService(session).load_snapshots(user.id)
    groups = {g.key: g for g in group_by(snapshots, "side")}

    assert set(groups) == {"LONG", "SHORT"}
    assert groups["LONG"].total_pnl == D("10.00")
    assert groups["SHORT"].total_pnl == D("-10.00")


async def test_mistake_impact_is_measured_in_money(ctx) -> None:  # type: ignore[no-untyped-def]
    user, journal, session = ctx
    base = datetime.now(UTC) - timedelta(hours=3)

    fomo = await MistakeTypeRepository(session).get_by_code("FOMO", user.id)
    assert fomo is not None

    for i, (entry, exit_price) in enumerate([("100", "80"), ("100", "70")]):
        trade = await _closed_trade(
            journal, user.id, "DOGE-USDT", TradeSide.LONG,
            entry, exit_price, "1", when=base + timedelta(hours=i),
        )
        session.add(TradeMistake(trade_id=trade.id, mistake_type_id=fomo.id))

    # Хорошая сделка без ошибки — для контраста
    await _closed_trade(journal, user.id, "BTC-USDT", TradeSide.LONG,
                        "100", "150", "1", when=base + timedelta(hours=3))
    await session.flush()

    impacts = await StatisticsService(session).mistake_impact(user.id)
    by_code = {i.code: i for i in impacts}

    assert "FOMO" in by_code
    assert by_code["FOMO"].occurrences == 2
    assert by_code["FOMO"].total_pnl == D("-50.00")
    assert by_code["FOMO"].win_rate == D("0.00")

    overall = await StatisticsService(session).overall_average_pnl(user.id)
    assert overall is not None
    # Сделки с FOMO хуже средней — это и есть вывод, который бот показывает
    assert by_code["FOMO"].average_pnl < overall


async def test_unannotated_excluded_from_strategy_breakdown(ctx) -> None:  # type: ignore[no-untyped-def]
    """Импортированные без разметки не попадают в срез по стратегиям."""
    user, journal, session = ctx
    from app.trading.enums import TradeSource

    base = datetime.now(UTC) - timedelta(hours=2)
    strategies = await StrategyRepository(session).list_active(user.id)

    manual = await journal.open_trade(
        user_id=user.id, symbol="BTC-USDT", side=TradeSide.LONG,
        entry_price=D("100"), quantity=D("1"),
        strategy_id=strategies[0].id, opened_at=base,
    )
    await journal.close_trade(manual, exit_price=D("110"), closed_at=base)

    imported = await journal.open_trade(
        user_id=user.id, symbol="ETH-USDT", side=TradeSide.LONG,
        entry_price=D("100"), quantity=D("1"),
        source=TradeSource.IMPORTED, opened_at=base,
    )
    await journal.close_trade(imported, exit_price=D("90"), closed_at=base)
    assert imported.is_annotated is False

    all_snaps = await StatisticsService(session).load_snapshots(user.id)
    annotated = await StatisticsService(session).load_snapshots(
        user.id, annotated_only=True
    )

    assert len(all_snaps) == 2
    assert len(annotated) == 1
    assert await StatisticsService(session).count_unannotated(user.id) == 1


async def test_period_filter_excludes_old_trades(ctx) -> None:  # type: ignore[no-untyped-def]
    user, journal, session = ctx

    # Якорь — середина сегодняшнего окна, а не now: сделка, закрытая
    # в 23:59 местного, иначе попадала бы в следующие сутки, и тест
    # падал бы в зависимости от времени запуска.
    start, end = period_bounds("today", tz_offset_hours=5)
    midday = start + (end - start) / 2

    old = start - timedelta(days=40)
    await _closed_trade(journal, user.id, "BTC-USDT", TradeSide.LONG,
                        "100", "110", "1", when=old)
    await _closed_trade(journal, user.id, "BTC-USDT", TradeSide.LONG,
                        "100", "120", "1", when=midday)

    today = await StatisticsService(session).load_snapshots(
        user.id, start=start, end=end
    )
    everything = await StatisticsService(session).load_snapshots(user.id)

    assert len(everything) == 2
    assert len(today) == 1
    assert today[0].pnl == D("20")


async def test_trade_closing_late_at_night_stays_in_same_local_day(ctx) -> None:  # type: ignore[no-untyped-def]
    """Регрессия: сделка в 23:45 по Перми — сегодняшний день, не завтрашний."""
    user, journal, session = ctx

    start, end = period_bounds("today", tz_offset_hours=5)
    late = end - timedelta(minutes=15)  # 23:45 местного

    trade = await journal.open_trade(
        user_id=user.id, symbol="BTC-USDT", side=TradeSide.LONG,
        entry_price=D("100"), quantity=D("1"), opened_at=late,
    )
    await journal.close_trade(trade, exit_price=D("105"), closed_at=late)

    today = await StatisticsService(session).load_snapshots(
        user.id, start=start, end=end
    )
    assert len(today) == 1
