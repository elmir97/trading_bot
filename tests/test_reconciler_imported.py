"""Reconciler для импортированных и ручных сделок (этап 3): закрытия фактом
биржи, граница «выходы после последнего исполнения», ручная запись без биржи
не трогается, уровни журнала следуют за openOrders. Харнесс и живые ответы
демо — из tests/test_reconciler_worker.py. На коде до этапа 3 импортированные
сделки не сверялись: тесты падают на поведении (сделка остаётся OPEN, уровни
пустые)."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.database.models.trade import Trade
from app.database.repositories.trade import TradeRepository
from app.trading.enums import ReconciliationKind, TradeSide, TradeSource, TradeStatus
from app.trading.journal import TradeJournal
from tests import test_reconciler_worker as harness
from tests.bingx_fixtures import LINK_MANUAL_STOP, live_items
from tests.test_reconciler_worker import (
    LINK_MANUAL_CHILD,
    MANUAL_STOP_REASON,
    FakeBot,
    _run_reconciler,
    _trade,
    _trade_events,
)

# Фикстура харнесса (настоящий BingXClient на живых ответах демо, БД).
ctx = harness.ctx

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

D = Decimal
LINK_OPENED = datetime(2026, 9, 27, 8, 15, 32, 828000, tzinfo=UTC)
LINK_POSITION_ID = "2104122757805514754"


async def _imported_link(  # type: ignore[no-untyped-def]
    session, user_id: int, *, fill_at: datetime = LINK_OPENED,
    source: TradeSource = TradeSource.IMPORTED, position_id: str | None = LINK_POSITION_ID,
    symbol: str = "LINK-USDT",
) -> Trade:
    """Сделка, заведённая импортом: исполнение с id allFillOrders (не orderId)."""
    trade = await TradeJournal(TradeRepository(session)).open_trade(
        user_id=user_id, symbol=symbol, side=TradeSide.LONG, entry_price=D("14.4"),
        quantity=D("2037.8"), leverage=10, opened_at=fill_at, source=source,
        external_fill_id="fill-tradeid-1", fee=D("14.672461"),
        external_position_id=position_id,
    )
    await session.commit()
    return trade


async def test_imported_trade_closed_by_manual_stop_on_exchange(ctx) -> None:  # type: ignore[no-untyped-def]
    settings, db, session, user, demo = ctx
    demo.positions = live_items("positions LINK (фильтр по символу на клиенте)", LINK_MANUAL_STOP)
    demo.all_orders = {"LINK-USDT": live_items("allOrders LINK", LINK_MANUAL_STOP)}
    link = await _imported_link(session, user.id)
    bot = FakeBot()

    await _run_reconciler(settings, db, bot)

    trade = await _trade(db, link.id)
    assert trade.status is TradeStatus.CLOSED
    assert trade.exit_price == D("14.776")
    assert trade.exit_reason == MANUAL_STOP_REASON
    assert {f.external_fill_id for f in trade.fills} == {"fill-tradeid-1", LINK_MANUAL_CHILD}
    [closed] = [
        e for e in await _trade_events(db, link.id)
        if e.kind is ReconciliationKind.CLOSED_OUTSIDE_BOT
    ]
    assert closed.notified_at is not None and len(bot.sent) == 1


async def test_exits_before_last_recorded_fill_are_not_counted(ctx) -> None:  # type: ignore[no-untyped-def]
    """Последнее исполнение в журнале позже ручного стопа (04:03:24) — этот
    выход уже учтён импортом под своим id, второй раз его не пишем."""
    settings, db, session, user, demo = ctx
    demo.positions = live_items("positions LINK (фильтр по символу на клиенте)", LINK_MANUAL_STOP)
    demo.all_orders = {"LINK-USDT": live_items("allOrders LINK", LINK_MANUAL_STOP)}
    link = await _imported_link(session, user.id, fill_at=datetime(2026, 9, 29, 5, 0, tzinfo=UTC))

    await _run_reconciler(settings, db, FakeBot())

    trade = await _trade(db, link.id)
    assert trade.status is TradeStatus.OPEN and len(trade.fills) == 1
    kinds = {e.kind for e in await _trade_events(db, link.id)}
    assert ReconciliationKind.CLOSED_OUTSIDE_BOT not in kinds


async def test_manual_journal_trade_without_exchange_is_untouched(ctx) -> None:  # type: ignore[no-untyped-def]
    settings, db, session, user, demo = ctx
    manual = await _imported_link(
        session, user.id, source=TradeSource.MANUAL, position_id=None, symbol="ADA-USDT"
    )

    await _run_reconciler(settings, db, FakeBot())

    trade = await _trade(db, manual.id)
    assert trade.status is TradeStatus.OPEN
    assert "/openApi/swap/v2/trade/allOrders" not in demo.calls
    assert await _trade_events(db, manual.id) == []


async def test_levels_follow_open_orders(ctx) -> None:  # type: ignore[no-untyped-def]
    """LINK открыта, на бирже стоп 13.526 и тейк 16.263 (живой ответ 27.09):
    журнал получает оба уровня, первый стоп — исходный (база 1R)."""
    settings, db, session, user, demo = ctx
    link = await _imported_link(session, user.id)

    await _run_reconciler(settings, db, FakeBot())

    trade = await _trade(db, link.id)
    assert trade.status is TradeStatus.OPEN
    assert trade.stop_loss == D("13.526") and trade.initial_stop_loss == D("13.526")
    assert trade.take_profit == D("16.263")
    assert demo.calls.count("/openApi/swap/v2/trade/openOrders") == 1  # один на все символы


async def test_moved_stop_keeps_initial_for_r(ctx) -> None:  # type: ignore[no-untyped-def]
    settings, db, session, user, demo = ctx
    link = await _imported_link(session, user.id)
    for order in demo.link_open_orders:
        if order["type"] == "STOP_MARKET":
            order["stopPrice"] = "14.4"  # стоп перенесён в безубыток
    async with db.session() as s:
        row = await s.get(Trade, link.id)
        assert row is not None
        row.stop_loss = D("13.526")
        await s.commit()

    await _run_reconciler(settings, db, FakeBot())

    trade = await _trade(db, link.id)
    assert trade.stop_loss == D("14.4")
    assert trade.initial_stop_loss == D("13.526") and trade.risk_stop == D("13.526")


async def test_live_link_records_position_id_then_stop_closes_trade(ctx) -> None:  # type: ignore[no-untyped-def]
    """02.10, #12: сделка из «В журнал» без positionId (в allFillOrders его
    нет). Пока позиция жива, reconciler записывает её positionId; стоп
    сработал, позиции нет — сделка закрывается фактом биржи одним выходом.
    На старом коде связь терялась с позицией, сделка оставалась OPEN."""
    settings, db, session, user, demo = ctx
    link = await _imported_link(session, user.id, position_id=None)

    await _run_reconciler(settings, db, FakeBot())          # позиция LINK жива
    trade = await _trade(db, link.id)
    assert trade.status is TradeStatus.OPEN
    assert trade.external_position_id == LINK_POSITION_ID

    demo.positions = live_items("positions LINK (фильтр по символу на клиенте)", LINK_MANUAL_STOP)
    demo.all_orders = {"LINK-USDT": live_items("allOrders LINK", LINK_MANUAL_STOP)}
    bot = FakeBot()
    await _run_reconciler(settings, db, bot)                 # стоп сработал

    trade = await _trade(db, link.id)
    assert trade.status is TradeStatus.CLOSED and trade.exit_price == D("14.776")
    assert [f.fill_side.value for f in trade.fills] == ["ENTRY", "EXIT"]
    assert len(bot.sent) == 1


async def test_button_import_closed_before_first_cycle_is_closed(ctx) -> None:  # type: ignore[no-untyped-def]
    """Вопрос владельца 02.10: позиция закрылась до первого цикла reconciler
    после импорта (связь по символу и стороне ни разу не увидена). Импорт
    кнопкой «В журнал» пишет positionId сразу — сделка закрывается."""
    settings, db, session, user, demo = ctx
    demo.positions = live_items("positions LINK (фильтр по символу на клиенте)", LINK_MANUAL_STOP)
    demo.all_orders = {"LINK-USDT": live_items("allOrders LINK", LINK_MANUAL_STOP)}
    link = await _imported_link(session, user.id, position_id=LINK_POSITION_ID)

    await _run_reconciler(settings, db, FakeBot())

    assert (await _trade(db, link.id)).status is TradeStatus.CLOSED


async def test_history_import_without_link_notifies_once(ctx) -> None:  # type: ignore[no-untyped-def]
    """Открытая сделка из /import (истории) без positionId, позиция закрылась
    до первого цикла reconciler: журнал не правится (чужие выходы приписать
    нельзя) — одно уведомление «сделка открыта в журнале, позиции нет»
    (AMBIGUOUS, дедуп по сделке; решение владельца 03.10). На старом коде —
    тишина, сделка висела OPEN."""
    settings, db, session, user, demo = ctx
    demo.positions = live_items("positions LINK (фильтр по символу на клиенте)", LINK_MANUAL_STOP)
    demo.all_orders = {"LINK-USDT": live_items("allOrders LINK", LINK_MANUAL_STOP)}
    link = await _imported_link(session, user.id, position_id=None)
    bot = FakeBot()

    await _run_reconciler(settings, db, bot)
    await _run_reconciler(settings, db, bot)

    assert (await _trade(db, link.id)).status is TradeStatus.OPEN
    [notice] = [e for e in await _trade_events(db, link.id)
                if e.dedup_key == f"journal_open:{link.id}"]
    assert notice.kind is ReconciliationKind.AMBIGUOUS and notice.notified_at is not None
    assert sum(f"сделка #{link.id} LONG открыта в журнале" in t for t in bot.sent) == 1

    # Сделку убрали из открытых (отменил вручную) — расхождение разрешено.
    async with db.session() as s:
        row = await s.get(Trade, link.id)
        row.status = TradeStatus.CANCELLED
    await _run_reconciler(settings, db, bot)
    [notice] = [e for e in await _trade_events(db, link.id)
                if e.dedup_key == f"journal_open:{link.id}"]
    assert notice.resolved_at is not None
