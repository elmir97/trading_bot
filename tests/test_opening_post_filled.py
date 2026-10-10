"""Деплой 3, фикс 2: маркет, POST вернул FILLED — защита сразу, без
подтверждения входа по cid (на Т3 10.10 оно не приходило 38 с). Цена и
комиссия входа дочитываются после стопа; не дочитались — PROTECTED
(«🛡 … итог следом»), сделку пишет цикл. Позиция может быть ещё не видна —
«позиции нет» только после повторных чтений и подтверждённого входа.
Аварийное закрытие — СНАЧАЛА, вход дочитывается после; не вышло —
EMERGENCY_CLOSED без сделки (ENTRY_FILL_UNREAD), сделку дописывает цикл
(решение владельца 10.10)."""

from __future__ import annotations

import os
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import update

from app.database.models.trade_opening import TradeOpening
from app.exchanges.base import ExchangeUnavailableError
from app.execution.opening.flow import ENTRY_FILL_UNREAD
from app.trading.enums import OpeningStatus, OrderRole, OrderStatus, TradeSide, TradeStatus
from tests.test_opening_confirm import _card, _rows, _trade, opening_context
from tests.test_opening_faults import _recover

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

D = Decimal


@pytest_asyncio.fixture
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    async for c in opening_context(unique_telegram_id()):
        yield c


def _entry_cid(c, opening_id: int) -> str:  # type: ignore[no-untyped-def]
    return f"to{opening_id}u{c.uid}e"


def _fail_entry_reads(c) -> None:  # type: ignore[no-untyped-def]
    """GET входа по cid падает (как на Т3), остальные чтения по cid — штатно."""
    original = c.exchange.get_order_fill

    async def get_order_fill(symbol: str, cid: str, **kw: Any):  # type: ignore[no-untyped-def]
        if cid.lower().endswith("e") and c.fail_entry:
            c.exchange.calls.append(("order_fill", cid))
            raise ExchangeUnavailableError("BingX не ответил вовремя")
        return await original(symbol, cid, **kw)

    c.fail_entry = True
    c.exchange.get_order_fill = get_order_fill


def _names_before(c, name: str) -> list[tuple[str, Any]]:  # type: ignore[no-untyped-def]
    """Вызовы до первого вызова name."""
    calls = c.exchange.calls
    index = next(i for i, (n, _) in enumerate(calls) if n == name)
    return calls[:index]


async def test_protects_without_entry_read_then_cycle_records(ctx) -> None:  # type: ignore[no-untyped-def]
    _fail_entry_reads(ctx)
    opening = await _card(ctx)
    out = await ctx.service().confirm(opening.id, accept_warnings=False)

    # Стоп поставлен, вход по cid до него не читали.
    before_stop = _names_before(ctx, "post_conditional")
    assert not [n for n, _ in before_stop if n == "order_fill"]
    assert out.status is OpeningStatus.PROTECTED and out.final
    assert out.text == (
        "🛡 Позиция XRP-USDT LONG 323 под стопом 1.4501 — цену входа биржа ещё не отдала, "
        "итог следом."
    )
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.PROTECTED and opening.trade_id is None
    assert opening.avg_price is None and opening.filled_qty == D(323)
    assert any(o.close_position and o.order_type == "STOP_MARKET" for o in ctx.exchange.orders)
    entry = next(r for r in await _rows(ctx, opening.id) if r.role is OrderRole.ENTRY)
    assert entry.status is OrderStatus.FILLED and entry.exchange_order_id

    # Цикл, биржа ещё молчит — сделки нет, сообщения нет.
    await _recover(ctx)
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.PROTECTED and ctx.notes == []

    # Биржа отдала вход — сделка с реальной ценой и комиссией, итог новым сообщением.
    ctx.fail_entry = False
    assert await _recover(ctx) == 1
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.DONE and opening.trade_id is not None
    assert opening.avg_price == D("1.4950") and opening.entry_fee is not None
    assert opening.position_id is not None
    trade = await _trade(ctx, opening.trade_id)
    assert trade.entry_price == D("1.4950") and trade.status is TradeStatus.OPEN
    assert ctx.notes[-1][1].startswith("✅ Открыто: XRP-USDT LONG 323 @ 1.495")
    assert ctx.notes[-1][2] == ("XRP-USDT", TradeSide.LONG)
    assert len([n for n, _ in ctx.exchange.posts() if n == "post_market"]) == 1


async def test_entry_read_ok_goes_straight_to_done(ctx) -> None:  # type: ignore[no-untyped-def]
    """Норма: стоп — до чтения входа, вход дочитан сразу после — DONE одним «Открыть»."""
    opening = await _card(ctx)
    out = await ctx.service().confirm(opening.id, accept_warnings=False)
    assert out.status is OpeningStatus.DONE and "@ 1.495" in out.text
    before_stop = _names_before(ctx, "post_conditional")
    assert not [n for n, _ in before_stop if n == "order_fill"]


async def test_position_visible_on_second_read(ctx) -> None:  # type: ignore[no-untyped-def]
    """Позиция не видна в первом чтении после POST — не «позиции нет»: повтор
    находит её, стоп ставится."""
    original = ctx.exchange.get_positions
    reads = {"n": 0}

    async def get_positions(**kw: Any):  # type: ignore[no-untyped-def]
        reads["n"] += 1
        result = await original(**kw)
        return [] if reads["n"] == 1 else result

    ctx.exchange.get_positions = get_positions
    opening = await _card(ctx)
    out = await ctx.service().confirm(opening.id, accept_warnings=False)
    assert out.status is OpeningStatus.DONE, out.text
    assert "Позиции на бирже уже нет" not in out.text
    assert any(o.close_position and o.order_type == "STOP_MARKET" for o in ctx.exchange.orders)


async def test_position_not_visible_and_entry_unread_waits(ctx) -> None:  # type: ignore[no-untyped-def]
    """Позиция не видна и вход не дочитан — ничего не решаем: FILLED, без стопа и
    без закрытия, перепроверка циклом."""
    _fail_entry_reads(ctx)
    original = ctx.exchange.get_positions
    ctx.hide = True

    async def get_positions(**kw: Any):  # type: ignore[no-untyped-def]
        result = await original(**kw)
        return [] if ctx.hide else result

    ctx.exchange.get_positions = get_positions
    opening = await _card(ctx)
    out = await ctx.service().confirm(opening.id, accept_warnings=False)
    assert out.status is OpeningStatus.FILLED
    assert out.text.startswith("⏳ Вход XRP-USDT LONG исполнен, но позиция на бирже ещё не видна")
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.FILLED
    posts = [n for n, _ in ctx.exchange.posts()]
    assert "post_conditional" not in posts and posts.count("post_market") == 1

    await _recover(ctx)                       # цикл: всё ещё не видна — молча ждём
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.FILLED and ctx.notes == []

    ctx.hide = False
    ctx.fail_entry = False
    await _recover(ctx)
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.DONE


async def test_post_not_filled_keeps_read_first(ctx) -> None:  # type: ignore[no-untyped-def]
    """POST маркета без FILLED — прежний путь: сначала подтверждение по cid."""
    original = ctx.exchange.place_market_order

    async def place_market_order(**kw: Any):  # type: ignore[no-untyped-def]
        return replace(await original(**kw), status="NEW")

    ctx.exchange.place_market_order = place_market_order
    opening = await _card(ctx)
    out = await ctx.service().confirm(opening.id, accept_warnings=False)
    assert out.status is OpeningStatus.DONE
    before_stop = _names_before(ctx, "post_conditional")
    assert ("order_fill", _entry_cid(ctx, opening.id)) in before_stop


async def test_emergency_close_first_then_cycle_records_trade(ctx) -> None:  # type: ignore[no-untyped-def]
    """Стоп не встал → закрытие СНАЧАЛА, без чтения входа до него; вход не
    дочитан и после — EMERGENCY_CLOSED без сделки + ERROR; цикл дописывает
    сделку со входом и выходом (решение владельца 10.10)."""
    ctx.exchange.drop_attached_sl = True
    ctx.exchange.fail_conditional_types = {"STOP_MARKET"}
    _fail_entry_reads(ctx)
    opening = await _card(ctx)
    out = await ctx.service().confirm(opening.id, accept_warnings=False)

    market_posts = [i for i, (n, _) in enumerate(ctx.exchange.calls) if n == "post_market"]
    close_at = market_posts[1]
    entry_reads = [i for i, (n, p) in enumerate(ctx.exchange.calls)
                   if n == "order_fill" and p == _entry_cid(ctx, opening.id)]
    assert entry_reads and min(entry_reads) > close_at        # закрыли до чтения входа
    assert not ctx.exchange.positions
    assert out.status is OpeningStatus.EMERGENCY_CLOSED and out.trade_id is None
    assert "Цену входа биржа ещё не отдала — сделку запишу в журнал следом." in out.text
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.EMERGENCY_CLOSED and opening.trade_id is None
    assert opening.error_code == ENTRY_FILL_UNREAD

    await _recover(ctx)                        # биржа молчит — ждём, без сообщения
    await ctx.session.refresh(opening)
    assert opening.trade_id is None and ctx.notes == []

    ctx.fail_entry = False
    await _recover(ctx)
    await ctx.session.refresh(opening)
    assert opening.trade_id is not None and opening.error_code == "STOP_FAILED"
    assert opening.status is OpeningStatus.EMERGENCY_CLOSED
    trade = await _trade(ctx, opening.trade_id)
    assert trade.status is TradeStatus.CLOSED and trade.entry_price == D("1.4950")
    assert trade.exit_reason == "Аварийное закрытие: стоп не встал"
    assert ctx.notes[-1][1].startswith(f"📒 Сделка #{opening.trade_id} записана в журнал")

    notes = len(ctx.notes)
    await _recover(ctx)                        # дописана — цикл её больше не берёт
    assert len(ctx.notes) == notes


async def test_old_emergency_without_trade_not_rewritten(ctx) -> None:  # type: ignore[no-untyped-def]
    """trade_id NULL у старого EMERGENCY_CLOSED (сделку удалили — SET NULL) —
    не повод дописывать: цикл берёт только ENTRY_FILL_UNREAD."""
    opening = await _card(ctx)
    await ctx.session.execute(
        update(TradeOpening).where(TradeOpening.id == opening.id).values(
            status=OpeningStatus.EMERGENCY_CLOSED, error_code="STOP_FAILED", trade_id=None,
        )
    )
    await ctx.session.commit()
    ctx.exchange.calls.clear()                 # карточка читала биржу
    assert await _recover(ctx) == 0
    assert ctx.exchange.calls == [] and ctx.notes == []
