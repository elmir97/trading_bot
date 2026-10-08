"""Карточки открытия и тревоги (08.10.2026, замечания владельца после Т0):
срок карточки 120 с; прежние карточки вытесняются новой (кнопки снимаются);
цикл переводит просроченные CARD в «устарела»; «Отмена»/«Изменить» — только
на текущей карточке; «стоп не подтверждён» — повторная тревога через 2 мин и
напоминание раз в 5 мин, без закрытия."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from sqlalchemy import select, update

from app.bot.handlers import open_trade
from app.bot.states.trade import OpenTradeStates
from app.database.models.trade_opening import TradeOpening
from app.execution.opening.recovery import expire_stale_cards, remind_unconfirmed
from app.trading.enums import OpeningSource, OpeningStatus
from tests.test_opening_confirm import INPUTS, _card, opening_context

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

NOW = datetime.now(UTC)


@pytest_asyncio.fixture
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    async for c in opening_context(unique_telegram_id()):
        yield c


async def _age(c, opening: TradeOpening, seconds: int) -> None:  # type: ignore[no-untyped-def]
    await c.session.execute(
        update(TradeOpening).where(TradeOpening.id == opening.id)
        .values(created_at=datetime.now(UTC) - timedelta(seconds=seconds))
    )
    await c.session.commit()


# --- срок карточки 120 с ----------------------------------------------------------------------


async def test_card_alive_at_90_seconds(ctx) -> None:  # type: ignore[no-untyped-def]
    opening = await _card(ctx)
    await _age(ctx, opening, 90)
    out = await ctx.service(exec_open_dry_run=True).confirm(opening.id, accept_warnings=False)
    assert out.status is OpeningStatus.DRY_RUN


async def test_card_expired_after_120_seconds(ctx) -> None:  # type: ignore[no-untyped-def]
    opening = await _card(ctx)
    await _age(ctx, opening, 125)
    out = await ctx.service().confirm(opening.id, accept_warnings=False)
    assert out.status is OpeningStatus.EXPIRED_CARD
    assert ctx.exchange.posts() == []


def test_card_text_says_120_seconds() -> None:
    from app.core.config import Settings

    assert Settings(  # type: ignore[call-arg]
        bot_token="1:x", database_url="postgresql+asyncpg://u:p@h/d", encryption_key="0" * 43 + "="
    ).exec_open_card_ttl_seconds == 120


# --- вытеснение и просрочка ----------------------------------------------------------------------


async def test_new_card_supersedes_previous(ctx) -> None:  # type: ignore[no-untyped-def]
    first = (await ctx.service().prepare(
        INPUTS, source=OpeningSource.WIZARD, chat_id=1, message_id=10
    )).opening
    refused = (await ctx.service().prepare(
        replace(INPUTS, leverage=50),
        source=OpeningSource.WIZARD, chat_id=1, message_id=11,
    )).opening
    second = (await ctx.service().prepare(
        INPUTS, source=OpeningSource.WIZARD, chat_id=1, message_id=12
    )).opening
    assert first and refused and second and refused.status is OpeningStatus.REFUSED
    stale = await ctx.service().supersede_cards(second.id, 12)
    assert sorted(stale) == [(1, 10), (1, 11)]
    await ctx.session.refresh(first)
    await ctx.session.refresh(refused)
    await ctx.session.refresh(second)
    assert first.status is OpeningStatus.EXPIRED_CARD and first.error_code == "SUPERSEDED"
    assert refused.status is OpeningStatus.REFUSED            # отказ остаётся отказом
    assert second.status is OpeningStatus.CARD


async def test_cycle_expires_stale_cards_only(ctx) -> None:  # type: ignore[no-untyped-def]
    old = await _card(ctx)
    fresh = await _card(ctx)
    await _age(ctx, old, 130)
    assert await expire_stale_cards(ctx.db, ctx.settings) >= 1
    await ctx.session.refresh(old)
    await ctx.session.refresh(fresh)
    assert old.status is OpeningStatus.EXPIRED_CARD and fresh.status is OpeningStatus.CARD


# --- «Отмена» / «Изменить» только на текущей ----------------------------------------------------


async def _form(card_opening_id: int, card_message_id: int) -> FSMContext:
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=1))
    await state.set_state(OpenTradeStates.confirm)
    await state.update_data({"card_opening_id": card_opening_id,
                             "card_message_id": card_message_id, "leverage": 10})
    return state


def _callback(data: str, message_id: int) -> MagicMock:
    message = MagicMock(spec=Message)
    message.message_id = message_id
    message.chat = SimpleNamespace(id=1)
    message.bot = SimpleNamespace(edit_message_reply_markup=AsyncMock())
    message.edit_text = AsyncMock()
    message.answer = AsyncMock()
    callback = MagicMock()
    callback.data = data
    callback.message = message
    callback.answer = AsyncMock()
    callback.id = "cb"
    return callback


async def test_decline_on_old_card_keeps_form(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(open_trade, "_audit", AsyncMock(return_value=True))

    def no_service(*a: Any, **k: Any) -> None:
        raise AssertionError("старая карточка не должна идти в ядро")

    monkeypatch.setattr(open_trade, "_service", no_service)
    state = await _form(card_opening_id=2, card_message_id=20)
    callback = _callback(f"{open_trade.OpenCB.NO}1", message_id=10)
    await open_trade.decline_open(callback, state, SimpleNamespace(id=1), None, None, None,  # type: ignore[arg-type]
                                  None, None)
    assert await state.get_state() == OpenTradeStates.confirm.state   # форма жива
    callback.answer.assert_awaited_once()
    assert "старая карточка" in callback.answer.call_args.args[0]
    callback.message.bot.edit_message_reply_markup.assert_awaited_once()
    callback.message.edit_text.assert_not_awaited()


async def test_edit_on_old_card_ignored(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    state = await _form(card_opening_id=2, card_message_id=20)
    callback = _callback(open_trade.OpenCB.EDIT, message_id=10)
    await open_trade.edit_card(  # type: ignore[arg-type]
        callback, state, SimpleNamespace(id=1), None, None, None, None,
    )
    assert await state.get_state() == OpenTradeStates.confirm.state   # не ушли к шагу стопа
    assert "старая карточка" in callback.answer.call_args.args[0]
    callback.message.edit_text.assert_not_awaited()


# --- «стоп не подтверждён»: 2 мин, потом раз в 5 мин ---------------------------------


async def test_unconfirmed_reminders(ctx) -> None:  # type: ignore[no-untyped-def]
    opening = await _card(ctx)
    await ctx.session.execute(
        update(TradeOpening).where(TradeOpening.id == opening.id)
        .values(status=OpeningStatus.ALARM, error_code="STOP_UNCONFIRMED")
    )
    await ctx.session.commit()
    await ctx.session.refresh(opening)
    sent: list[str] = []

    async def notify(telegram_id: int, text: str, position: Any) -> None:
        sent.append(text)

    t0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    for seconds in (0, 60, 125, 300, 430, 600, 740):
        await remind_unconfirmed(ctx.redis, ctx.settings, opening, 1, notify,
                                 t0 + timedelta(seconds=seconds))
    assert sent[0] == (
        "🚨 Стоп не подтверждён 2 мин — проверь позицию XRP-USDT LONG на бирже вручную."
    )
    assert len(sent) == 3            # 125 с; 430 с (через 5 мин); 740 с
    assert "(7 мин)" in sent[1] and "(12 мин)" in sent[2]
    assert "не закрывает" in sent[1]
    # решилось — ключи сняты, напоминаний больше нет
    opening.status = OpeningStatus.DONE
    await remind_unconfirmed(ctx.redis, ctx.settings, opening, 1, notify,
                             t0 + timedelta(seconds=1000))
    assert await ctx.redis.exists(f"opening:unconfirmed:{opening.id}") == 0
    assert len(sent) == 3


async def test_unconfirmed_cycle_never_closes(ctx) -> None:  # type: ignore[no-untyped-def]
    """Стоп так и не подтверждается: циклы шлют напоминания, позицию не закрывают."""
    from app.execution.opening.recovery import recover_openings

    ctx.exchange.drop_attached_sl = True
    ctx.exchange.conditional_invisible_in_list = True
    opening = await _card(ctx)
    original = ctx.exchange.place_conditional_order

    async def cond_then_fail_reads(**kw: Any):  # type: ignore[no-untyped-def]
        result = await original(**kw)
        ctx.exchange.fail_reads = {"order_fill"}
        return result

    ctx.exchange.place_conditional_order = cond_then_fail_reads  # type: ignore[method-assign]
    out = await ctx.service().confirm(opening.id, accept_warnings=False)
    assert out.status is OpeningStatus.ALARM
    notes: list[str] = []

    async def notify(telegram_id: int, text: str, position: Any) -> None:
        notes.append(text)

    t0 = datetime.now(UTC)
    for seconds in (5, 130, 200, 440):
        await recover_openings(ctx.db, ctx.settings, ctx.redis, ctx.factory, notify,
                               now=t0 + timedelta(seconds=seconds))
    assert any(n.startswith("🚨 Стоп не подтверждён 2 мин") for n in notes)
    assert any("всё ещё не подтверждён" in n for n in notes)
    assert ctx.exchange.positions                                   # не закрыта
    assert [n for n, kw in ctx.exchange.posts() if n == "post_market"] == ["post_market"]
    row = await ctx.session.scalar(select(TradeOpening).where(TradeOpening.id == opening.id))
    assert row is not None
    await ctx.session.refresh(row)
    assert row.status is OpeningStatus.ALARM
