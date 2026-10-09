"""Лимитный вход открытия (app/execution/opening/limits.py): постановка,
исполнение, частичное, истечение по сроку бота, отмена из бота, гонка
«отмена ↔ исполнение», рестарт с WORKING-лимитом. Фикстура — из
test_opening_confirm (БД, FakeRedis, фейковая биржа)."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from app.execution.opening.calc import OpeningInputs
from app.execution.opening.recovery import opening_in_flight
from app.trading.enums import (
    CancelSource,
    EntryType,
    FillSide,
    OpeningStatus,
    OrderRole,
    OrderStatus,
    TradeSide,
    TradeStatus,
)
from tests.test_opening_confirm import (
    INPUTS,
    _card,
    _posts,
    _rows,
    _trade,
    opening_context,
)

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

D = Decimal
LIMIT: OpeningInputs = replace(
    INPUTS, entry_type=EntryType.LIMIT, limit_price=D("1.4800"), stop_loss=D("1.4500"),
    take_profit=D("1.5400"), expiry_minutes=240,
)


@pytest_asyncio.fixture
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    async for c in opening_context(unique_telegram_id()):
        yield c


async def _working(c):  # type: ignore[no-untyped-def]
    opening = await _card(c, LIMIT)
    out = await c.service().confirm(opening.id, accept_warnings=False)
    assert out.status is OpeningStatus.WORKING, out.text
    await c.session.refresh(opening)
    return opening, out


async def test_limit_placed_working(ctx) -> None:  # type: ignore[no-untyped-def]
    opening, out = await _working(ctx)
    assert "⏳ Лимит XRP-USDT LONG" in out.text and "Срок 4 ч" in out.text
    kw = next(kw for name, kw in ctx.exchange.posts() if name == "post_limit")
    assert kw["price"] == D("1.4800") and kw["stop_loss"].trigger_price == D("1.4500")
    assert kw["client_order_id"] == f"to{opening.id}u{ctx.uid}e"
    assert opening.expires_at is not None
    left = opening.expires_at - datetime.now(UTC)
    assert timedelta(minutes=239) < left <= timedelta(minutes=240)
    [entry] = await _rows(ctx, opening.id)
    assert entry.status is OrderStatus.WORKING and entry.exchange_order_id is not None
    assert await opening_in_flight(ctx.session, ctx.uid, "XRP-USDT", TradeSide.LONG)


async def test_resting_limit_untouched_before_expiry(ctx) -> None:  # type: ignore[no-untyped-def]
    opening, _ = await _working(ctx)
    assert await ctx.recover() == 0
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.WORKING and "post_cancel" not in _posts(ctx)


async def test_expiry_cancels_by_bot_timer(ctx) -> None:  # type: ignore[no-untyped-def]
    opening, _ = await _working(ctx)
    assert await ctx.recover(now=datetime.now(UTC) + timedelta(hours=4, minutes=1)) == 1
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.EXPIRED and opening.trade_id is None
    assert "post_cancel" in _posts(ctx)
    text = ctx.notes[-1][1]
    assert text.startswith("⌛ Срок лимита вышел через 4 ч") and "Позиция не открыта" in text
    [entry] = await _rows(ctx, opening.id)
    assert entry.status is OrderStatus.CANCELLED
    assert entry.cancel_source is CancelSource.BOT                 # A.3: срок держит бот
    assert not await opening_in_flight(ctx.session, ctx.uid, "XRP-USDT", TradeSide.LONG)


async def test_cancel_from_bot(ctx) -> None:  # type: ignore[no-untyped-def]
    opening, _ = await _working(ctx)
    out = await ctx.service().cancel_limit(opening.id)
    assert out.status is OpeningStatus.CANCELLED and "Лимит отменён" in out.text
    again = await ctx.service().cancel_limit(opening.id)
    assert not again.final and again.text == "Лимит отменён."
    assert _posts(ctx).count("post_cancel") == 1
    [entry] = await _rows(ctx, opening.id)
    assert entry.status is OrderStatus.CANCELLED
    assert entry.cancel_source is CancelSource.USER                # A.3: кнопка владельца


async def test_full_fill_creates_trade(ctx) -> None:  # type: ignore[no-untyped-def]
    opening, _ = await _working(ctx)
    ctx.exchange.fill_limit(f"to{opening.id}u{ctx.uid}e")
    assert await ctx.recover() == 1
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.DONE and opening.avg_price == D("1.4800")
    trade = await _trade(ctx, opening.trade_id)
    assert trade.quantity == opening.filled_qty and trade.status is TradeStatus.OPEN
    assert "✅ Открыто" in ctx.notes[-1][1]
    assert ctx.notes[-1][2] == ("XRP-USDT", TradeSide.LONG)


async def test_partial_fills_protected_then_full(ctx) -> None:  # type: ignore[no-untyped-def]
    opening, _ = await _working(ctx)
    cid = f"to{opening.id}u{ctx.uid}e"
    total = opening.quantity
    ctx.exchange.fill_limit(cid, D(100))
    await ctx.recover()
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.WORKING and opening.filled_qty == D(100)
    assert f"исполнен частично: 100 из {int(total)}" in ctx.notes[-1][1]
    # повтор цикла без нового исполнения — без сообщения
    sent = len(ctx.notes)
    await ctx.recover()
    assert len(ctx.notes) == sent

    # вторая часть: вложенные стопы по 100 — меньше позиции 200 → запасной на всё
    ctx.exchange.fill_limit(cid, D(100))
    await ctx.recover()
    def stop_posts():  # type: ignore[no-untyped-def]
        return [kw for name, kw in ctx.exchange.posts()
                if name == "post_conditional" and kw["order_type"] == "STOP_MARKET"]

    # closePosition-стоп поставлен на первой части (заменил вложенный на 100)
    assert len(stop_posts()) == 1 and stop_posts()[0]["close_position"] is True
    assert stop_posts()[0]["quantity"] == D(100)
    # новые части: вложенные снимаются, второй closePosition не ставится (110406)
    ctx.exchange.fill_limit(cid, D(50))
    await ctx.recover()
    assert len(stop_posts()) == 1
    stops = [o for o in ctx.exchange.orders if o.order_type == "STOP_MARKET"]
    assert len(stops) == 1 and stops[0].close_position

    ctx.exchange.fill_limit(cid)   # остаток
    await ctx.recover()
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.DONE and opening.filled_qty == total
    trade = await _trade(ctx, opening.trade_id)
    assert trade.quantity == total


async def test_partial_then_cancel_records_filled_part(ctx) -> None:  # type: ignore[no-untyped-def]
    opening, _ = await _working(ctx)
    ctx.exchange.fill_limit(f"to{opening.id}u{ctx.uid}e", D(100))
    await ctx.recover()
    out = await ctx.service().cancel_limit(opening.id)
    assert out.status is OpeningStatus.DONE, out.text
    assert f"исполнено 100 из {int(opening.quantity)}, остаток снят" in out.text
    trade = await _trade(ctx, out.trade_id)
    assert trade.quantity == D(100)
    assert [f.fill_side for f in trade.fills] == [FillSide.ENTRY]


async def test_partial_then_expiry(ctx) -> None:  # type: ignore[no-untyped-def]
    opening, _ = await _working(ctx)
    ctx.exchange.fill_limit(f"to{opening.id}u{ctx.uid}e", D(100))
    await ctx.recover()
    await ctx.recover(now=datetime.now(UTC) + timedelta(hours=5))
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.DONE and opening.filled_qty == D(100)
    assert "Срок лимита вышел: исполнено 100" in ctx.notes[-1][1]


async def test_cancel_races_fill(ctx) -> None:  # type: ignore[no-untyped-def]
    """Лимит исполнился, пока жали «Отменить»: биржа отвечает «ордера нет» —
    сделка записывается, ничего не теряется."""
    opening, _ = await _working(ctx)
    ctx.exchange.fill_limit(f"to{opening.id}u{ctx.uid}e")
    out = await ctx.service().cancel_limit(opening.id)
    assert out.status is OpeningStatus.DONE
    assert out.text.startswith("Лимит исполнился раньше отмены.")


async def test_marketable_limit_fills_at_once(ctx) -> None:  # type: ignore[no-untyped-def]
    """Р6: лимит сквозь рынок исполняется сразу — сделка в том же «Открыть»."""
    opening = await _card(ctx, replace(LIMIT, limit_price=D("1.4976"), stop_loss=D("1.4501"),
                                       take_profit=D("1.5399")))
    out = await ctx.service().confirm(opening.id, accept_warnings=False)
    assert out.status is OpeningStatus.DONE, out.text
    rows = await _rows(ctx, opening.id)
    assert {r.role for r in rows} >= {OrderRole.ENTRY, OrderRole.STOP_LOSS}


async def test_manual_cancel_on_exchange(ctx) -> None:  # type: ignore[no-untyped-def]
    opening, _ = await _working(ctx)
    await ctx.exchange.cancel_order_by_client_id("XRP-USDT", f"to{opening.id}u{ctx.uid}e")
    await ctx.recover()
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.CANCELLED
    assert "Лимит снят на бирже" in ctx.notes[-1][1]
    [entry] = await _rows(ctx, opening.id)
    await ctx.session.refresh(entry)
    # A.3: сняли не мы — кто, неизвестно: CANCELLED + NULL.
    assert entry.status is OrderStatus.CANCELLED and entry.cancel_source is None


async def test_active_opening_symbol_not_orphan(ctx) -> None:  # type: ignore[no-untyped-def]
    """Позиция частично исполненного лимита ещё без сделки — reconciler не
    шлёт ORPHAN_POSITION: символ активного открытия «в полёте»."""
    from app.workers.reconciler import _opening_symbols

    opening, _ = await _working(ctx)
    assert await _opening_symbols(ctx.session, ctx.uid) == {"XRP-USDT"}
    await ctx.service().cancel_limit(opening.id)
    assert await _opening_symbols(ctx.session, ctx.uid) == set()
