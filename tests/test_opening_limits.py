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


# --- A.1 / Л3 (10.10.2026): выход из WORKING — ⏳ правится, кнопка снимается ----------


async def test_exit_from_working_retires_card(ctx) -> None:  # type: ignore[no-untyped-def]
    """Истечение и исполнение циклом: notify получает статус и id открытия,
    чей ⏳ надо исправить; частичное исполнение (остаётся WORKING) — нет."""
    opening, _ = await _working(ctx)
    assert await ctx.recover(now=datetime.now(UTC) + timedelta(hours=4, minutes=1)) == 1
    assert ctx.kws[-1]["retire_working"] == opening.id
    assert ctx.kws[-1]["status"] is OpeningStatus.EXPIRED

    filled, _ = await _working(ctx)
    ctx.exchange.fill_limit(f"to{filled.id}u{ctx.uid}e")
    assert await ctx.recover() == 1
    assert ctx.kws[-1]["retire_working"] == filled.id
    assert ctx.kws[-1]["status"] is OpeningStatus.DONE


async def test_partial_fill_keeps_card(ctx) -> None:  # type: ignore[no-untyped-def]
    opening, _ = await _working(ctx)
    ctx.exchange.fill_limit(f"to{opening.id}u{ctx.uid}e", qty=D("40"))
    await ctx.recover()
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.WORKING
    assert all(kw.get("retire_working") is None for kw in ctx.kws)


class _Bot:
    """Бот для OpeningsWorker: правки ⏳ и новые сообщения."""

    def __init__(self, fail_edit: bool = False) -> None:
        self.edits: list[tuple[int, str, object]] = []
        self.sent: list[tuple[str, object]] = []
        self.fail_edit = fail_edit
        self.next_id = 900

    async def edit_message_text(self, text, *, chat_id, message_id, reply_markup=None):  # type: ignore[no-untyped-def]
        from unittest.mock import MagicMock

        from aiogram.exceptions import TelegramBadRequest

        if self.fail_edit:
            raise TelegramBadRequest(method=MagicMock(), message="message to edit not found")
        self.edits.append((message_id, text, reply_markup))

    async def send_message(self, chat_id, text, reply_markup=None, **_):  # type: ignore[no-untyped-def]
        from aiogram.types import Chat, Message

        self.next_id += 1
        self.sent.append((text, reply_markup))
        return Message(message_id=self.next_id, date=datetime.now(UTC),
                       chat=Chat(id=chat_id, type="private"), text=text)


async def _retire(ctx, bot: _Bot, status: OpeningStatus):  # type: ignore[no-untyped-def]
    from app.bot import outbox
    from app.database.models.outgoing_message import OutgoingMeta
    from app.workers.openings import OpeningsWorker

    opening, _ = await _working(ctx)
    chat_id = ctx.user.telegram_id
    opening.chat_id, opening.card_message_id = chat_id, 485
    await ctx.session.commit()
    meta = OutgoingMeta(user_id=ctx.uid, kind="OPEN_WORKING", trade_opening_id=opening.id)
    await outbox.record(ctx.db, meta, chat_id=chat_id, message_id=485, text="⏳ Лимит")
    worker = OpeningsWorker(bot, ctx.db, ctx.settings, None, None, factory=object())  # type: ignore[arg-type]
    done = OutgoingMeta(user_id=ctx.uid, kind=f"OPEN_{status.value}",
                        trade_opening_id=opening.id)
    await worker.notify(chat_id, "⌛ Срок лимита вышел", None, meta=done, status=status,
                        retire_working=opening.id)
    return opening, chat_id


async def test_worker_retires_working_card(ctx) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from app.database.models.outgoing_message import OutgoingMessage

    bot = _Bot()
    opening, _chat_id = await _retire(ctx, bot, OpeningStatus.EXPIRED)
    [(message_id, text, markup)] = bot.edits
    assert message_id == 485 and markup is None             # кнопка «Отменить лимит» снята
    assert text == "⏳ Лимит XRP-USDT LONG @ 1.48: ⌛ срок вышел, лимит снят."
    assert bot.sent == [("⌛ Срок лимита вышел", None)]      # полный итог — новым сообщением
    async with ctx.db.session() as session:
        rows = {r.message_id: r for r in await session.scalars(
            select(OutgoingMessage).where(OutgoingMessage.trade_opening_id == opening.id)
        )}
    assert rows[485].kind == "OPEN_WORKING_EXPIRED" and rows[485].text == text
    assert [e["text"] for e in rows[485].edits] == ["⏳ Лимит"]
    assert rows[901].kind == "OPEN_EXPIRED"


async def test_worker_retire_failure_still_sends(ctx) -> None:  # type: ignore[no-untyped-def]
    """«message to edit not found» (удалено, старше 48 ч) — WARNING, цикл не
    падает, полный итог уходит."""
    bot = _Bot(fail_edit=True)
    await _retire(ctx, bot, OpeningStatus.DONE)
    assert bot.edits == [] and len(bot.sent) == 1


def test_working_exit_texts() -> None:
    from types import SimpleNamespace

    from app.execution.opening.flow import working_exit_text

    o = SimpleNamespace(symbol="XRP-USDT", side=TradeSide.LONG, limit_price=D("1.3218"))
    assert working_exit_text(o, OpeningStatus.DONE) == (  # type: ignore[arg-type]
        "⏳ Лимит XRP-USDT LONG @ 1.3218: ✅ исполнен — итог ниже."
    )
    assert "снят на бирже" in working_exit_text(o, OpeningStatus.CANCELLED)  # type: ignore[arg-type]
    assert "завершён" in working_exit_text(o, OpeningStatus.NOT_PLACED)  # type: ignore[arg-type]
