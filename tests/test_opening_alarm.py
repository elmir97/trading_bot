"""ALARM (A.1, 10.10.2026): «🔴 Закрыть маркетом» через ядро открытия (одно
подтверждение «Да, закрыть», итог EMERGENCY_CLOSED со сделкой), сбой
fail_backup_stop_always (ALARM держится до кнопки; только демо), итог после
тревоги «со N-й попытки — позиция была без стопа M с», сообщение тревоги →
«✅ Решено …». Фейковая биржа и БД — test_opening_confirm.opening_context."""

from __future__ import annotations

import asyncio
import os
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiogram.types import CallbackQuery, Message
from pydantic import ValidationError

from app.bot import opening_messages, outbox
from app.bot.handlers import open_trade
from app.bot.handlers.open_trade import OpenCB, opening_keyboard
from app.core.config import Settings
from app.database.models.outgoing_message import OutgoingMessage, OutgoingMeta
from app.trading.enums import OpeningStatus, OrderRole, OrderStatus, TradeSide, TradeStatus
from tests.test_opening_confirm import _rows, _trade, opening_context
from tests.test_opening_faults import BASE, _open, _posts, _recover, _stops_on_exchange

needs_db = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")
ALWAYS = "skip_attached_stop,fail_backup_stop_always"
T3 = "skip_attached_stop,fail_backup_stop,fail_emergency_close"


@pytest_asyncio.fixture
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    async for c in opening_context(unique_telegram_id()):
        yield c


# --- сбой fail_backup_stop_always: только демо -------------------------------------------


def test_always_fault_accepted_on_demo() -> None:
    s = Settings(**BASE, exec_open_fault=ALWAYS)  # type: ignore[arg-type]
    assert "fail_backup_stop_always" in s.exec_open_faults


@pytest.mark.parametrize("live", [
    {"exec_open_allow_live": True},
    {"bingx_trading_mode": "live"},
    {"exec_allow_live_mode_orders": True},
])
def test_always_fault_refused_outside_demo(live: dict[str, Any]) -> None:
    """Решение владельца 10.10: с этим сбоем бот не стартует при allow_live или
    режиме не demo (проверка настроек на старте — Settings)."""
    with pytest.raises(ValidationError, match="только на демо"):
        Settings(**BASE, exec_open_fault="fail_backup_stop_always", **live)  # type: ignore[arg-type]


@needs_db
async def test_always_fault_keeps_alarm_until_button(ctx) -> None:  # type: ignore[no-untyped-def]
    """ALARM держится: каждый цикл — стоп и автоматическое закрытие «отклонены»,
    на бирже ни стопа, ни закрытия; «Да, закрыть» закрывает позицию."""
    opening, out = await _open(ctx, ALWAYS)
    assert out.status is OpeningStatus.ALARM and "БЕЗ СТОПА" in out.text
    for _ in range(3):
        await _recover(ctx, ALWAYS)
        await ctx.session.refresh(opening)
        assert opening.status is OpeningStatus.ALARM
    assert ctx.exchange.positions and not _stops_on_exchange(ctx)
    assert _posts(ctx, "post_conditional") == [] and len(_posts(ctx, "post_market")) == 1

    result = await ctx.service(exec_open_fault=ALWAYS).close_alarm(opening.id)
    assert result.status is OpeningStatus.EMERGENCY_CLOSED and result.final
    assert "Закрыто кнопкой из тревоги" in result.text and f"#{result.trade_id}" in result.text
    assert not ctx.exchange.positions
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.EMERGENCY_CLOSED
    trade = await _trade(ctx, result.trade_id)
    assert trade.status is TradeStatus.CLOSED
    assert trade.exit_reason == "Закрыто кнопкой из тревоги: стоп не встал"
    closes = [r for r in await _rows(ctx, opening.id) if r.role is OrderRole.CLOSE]
    assert closes[-1].status is OrderStatus.FILLED                 # ручное — без сбоя
    assert len(closes) == 5 and all(r.error_code == "FAULT" for r in closes[:-1])  # «Да» + 3 цикла


@needs_db
async def test_close_alarm_after_stop_placed_refuses(ctx) -> None:  # type: ignore[no-untyped-def]
    """Стоп встал циклом раньше нажатия — закрывать не нужно, на биржу ничего."""
    opening, _ = await _open(ctx, T3)
    await _recover(ctx, T3)
    markets = len(_posts(ctx, "post_market"))
    result = await ctx.service().close_alarm(opening.id)
    assert not result.final and result.status is OpeningStatus.DONE
    assert result.text.startswith("Стоп уже стоит")
    assert len(_posts(ctx, "post_market")) == markets and ctx.exchange.positions


@needs_db
async def test_close_alarm_without_position(ctx) -> None:  # type: ignore[no-untyped-def]
    opening, _ = await _open(ctx, ALWAYS)
    ctx.exchange.positions.clear()
    result = await ctx.service(exec_open_fault=ALWAYS).close_alarm(opening.id)
    assert not result.final and "Позиции на бирже уже нет" in result.text
    assert len(_posts(ctx, "post_market")) == 1


# --- итог после тревоги ---------------------------------------------------------------------


@needs_db
async def test_done_after_alarm_names_attempt_and_time(ctx) -> None:  # type: ignore[no-untyped-def]
    """Т3: s1 отклонён, закрытие отклонено → ALARM → цикл ставит s2: «со 2-й
    попытки — позиция была без стопа N с»; notify — снять тревогу."""
    opening, _ = await _open(ctx, T3)
    await _recover(ctx, T3)
    text = ctx.notes[-1][1]
    assert ("(на всю позицию, поставлен отдельным ордером — вложенного не было; со 2-й "
            "попытки — позиция была без стопа") in text, text
    assert ctx.kws[-1]["resolve_alarm"] == opening.id
    assert ctx.kws[-1]["status"] is OpeningStatus.DONE


@needs_db
async def test_unprotected_time_is_until_stop_accepted(ctx) -> None:  # type: ignore[no-untyped-def]
    """Деплой 3, фикс 4: «без стопа N с» — до ответа биржи на POST стопа, а не до
    записи итога (на Т3 тейк и снятие вложенных после стопа добавили 2.4 с)."""
    opening, _ = await _open(ctx, T3)
    original = ctx.exchange.place_conditional_order
    accepted: list[datetime] = []

    async def place(**kw: Any):  # type: ignore[no-untyped-def]
        if kw["order_type"] == "TAKE_PROFIT_MARKET":
            await asyncio.sleep(0.5)          # тейк после стопа — медленно
        result = await original(**kw)
        if kw["order_type"] == "STOP_MARKET":
            accepted.append(datetime.now(UTC))
        return result

    ctx.exchange.place_conditional_order = place
    await _recover(ctx, T3)
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.DONE and len(accepted) == 1
    match = re.search(r"без стопа (\d+\.\d) с", ctx.notes[-1][1])
    assert match, ctx.notes[-1][1]
    expected = (accepted[0] - opening.filled_at).total_seconds()
    assert abs(float(match.group(1)) - expected) <= 0.15, (match.group(1), expected)


@needs_db
async def test_done_after_unconfirmed_stop_says_rechecked(ctx) -> None:  # type: ignore[no-untyped-def]
    """Т5: s1 стоял, но не был виден — «подтверждён повторной проверкой»."""
    fault = "skip_attached_stop,hide_backup_stop,fail_emergency_close"
    opening, out = await _open(ctx, fault)
    assert out.status is OpeningStatus.ALARM
    await _recover(ctx, fault)
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.DONE
    assert "подтверждён повторной проверкой через" in ctx.notes[-1][1]
    assert "без стопа" not in ctx.notes[-1][1]


@needs_db
async def test_alarm_from_cycle_has_position_button(ctx) -> None:  # type: ignore[no-untyped-def]
    """ALARM, объявленный циклом, — с «Позиция»: notify получает позицию."""
    opening, _ = await _open(ctx, T3)
    # снова FILLED: защиту ведёт цикл, ALARM — его итог
    opening.status = OpeningStatus.FILLED
    await ctx.session.commit()
    await _recover(ctx, ALWAYS)
    assert ctx.kws[-1]["status"] is OpeningStatus.ALARM
    assert ctx.notes[-1][2] == ("XRP-USDT", TradeSide.LONG)
    assert ctx.kws[-1]["resolve_alarm"] is None


# --- клавиатура и сообщение тревоги -----------------------------------------------------


def _labels(markup: Any) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def test_alarm_keyboard() -> None:
    kb = opening_keyboard(OpeningStatus.ALARM, 15, ("XRP-USDT", TradeSide.LONG))
    assert kb is not None
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert data == ["pos:act:XRP-USDT:L", f"{OpenCB.CLOSE}15"]
    assert kb.inline_keyboard[-1][0].text == "🔴 Закрыть маркетом"
    result = open_trade.result_keyboard(
        open_trade.ConfirmOutcome("🚨🚨", OpeningStatus.ALARM), 15, "XRP-USDT", TradeSide.LONG
    )
    assert result == kb


def test_close_prefixes_do_not_collide() -> None:
    assert not f"{OpenCB.CLOSE_YES}5".startswith(OpenCB.CLOSE)
    assert not f"{OpenCB.CLOSE_NO}5".startswith(OpenCB.CLOSE)
    assert not f"{OpenCB.CLOSE_NO}5".startswith(OpenCB.NO)
    assert not f"{OpenCB.CLOSE_YES}5".startswith(OpenCB.YES)


class _Bot:
    def __init__(self) -> None:
        self.edits: list[tuple[int, str, object]] = []

    async def edit_message_text(self, text, *, chat_id, message_id, reply_markup=None):  # type: ignore[no-untyped-def]
        self.edits.append((message_id, text, reply_markup))


@needs_db
async def test_resolve_alarm_rewrites_alarm_message(ctx) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    opening, _ = await _open(ctx, ALWAYS)
    chat_id = ctx.user.telegram_id
    meta = OutgoingMeta(user_id=ctx.uid, kind="OPEN_ALARM", trade_opening_id=opening.id)
    await outbox.record(ctx.db, meta, chat_id=chat_id, message_id=520, text="🚨🚨 БЕЗ СТОПА")
    reminder = OutgoingMeta(user_id=ctx.uid, kind="OPEN_ALARM_REMINDER",
                            trade_opening_id=opening.id)
    await outbox.record(ctx.db, reminder, chat_id=chat_id, message_id=521, text="🚨 напоминание")
    bot = _Bot()
    at = datetime(2026, 10, 10, 14, 23, 42, tzinfo=UTC)
    assert await opening_messages.resolve_alarm(
        bot, ctx.db, opening.id, OpeningStatus.DONE, now=at  # type: ignore[arg-type]
    ) == 1
    [(message_id, text, markup)] = bot.edits
    assert message_id == 520 and markup is None                  # «🔴 Закрыть» снята
    assert text == ("🚨 → ✅ Решено: стоп поставлен в 19:23 — позиция XRP-USDT LONG под "
                    "защитой, итог ниже.")
    async with ctx.db.session() as session:
        row = await session.scalar(select(OutgoingMessage).where(
            OutgoingMessage.chat_id == chat_id, OutgoingMessage.message_id == 520
        ))
    assert row is not None and row.kind == "OPEN_ALARM_DONE"
    assert [e["text"] for e in row.edits] == ["🚨🚨 БЕЗ СТОПА"]
    # повтор — сообщений OPEN_ALARM больше нет, ничего не правится
    assert await opening_messages.resolve_alarm(
        bot, ctx.db, opening.id, OpeningStatus.DONE  # type: ignore[arg-type]
    ) == 0


def test_alarm_resolved_texts() -> None:
    o = SimpleNamespace(symbol="XRP-USDT", side=TradeSide.LONG)
    closed = opening_messages.alarm_resolved_text(o, OpeningStatus.EMERGENCY_CLOSED, "21:41")  # type: ignore[arg-type]
    assert closed == "🚨 → ✅ Решено: позиция XRP-USDT LONG закрыта аварийно в 21:41 — итог ниже."


# --- обработчики ----------------------------------------------------------------------------


def _callback(data: str, message_id: int = 520) -> MagicMock:
    message = MagicMock(spec=Message)
    message.bot = MagicMock()
    message.chat = SimpleNamespace(id=777)
    message.message_id = message_id
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.message = message
    callback.answer = AsyncMock()
    return callback


def _patch(monkeypatch, opening: Any, close_result: Any = None) -> MagicMock:  # type: ignore[no-untyped-def]
    service = MagicMock()
    service.load = AsyncMock(return_value=opening)
    service.close_alarm = AsyncMock(return_value=close_result)
    monkeypatch.setattr(open_trade, "_service", lambda *a, **k: service)
    monkeypatch.setattr(open_trade, "_audit", AsyncMock(return_value=True))
    return service


async def test_red_button_asks_in_new_message(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Сообщение тревоги — итог: подтверждение новым сообщением, тревога цела."""
    alarm = SimpleNamespace(id=15, status=OpeningStatus.ALARM, symbol="XRP-USDT",
                            side=TradeSide.LONG, trade_id=None)
    _patch(monkeypatch, alarm)
    cb = _callback(f"{OpenCB.CLOSE}15")
    await open_trade.ask_close_alarm(cb, MagicMock(), MagicMock(), MagicMock(), MagicMock(),
                                     MagicMock(), MagicMock())
    cb.message.edit_text.assert_not_awaited()
    [call] = cb.message.answer.await_args_list
    assert "Закрыть XRP-USDT LONG маркетом?" in call.args[0]
    data = [b.callback_data for b in call.kwargs["reply_markup"].inline_keyboard[0]]
    assert data == [f"{OpenCB.CLOSE_YES}15", f"{OpenCB.CLOSE_NO}15"]


async def test_red_button_after_resolution_alerts(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    done = SimpleNamespace(id=15, status=OpeningStatus.DONE, trade_id=23)
    _patch(monkeypatch, done)
    cb = _callback(f"{OpenCB.CLOSE}15")
    await open_trade.ask_close_alarm(cb, MagicMock(), MagicMock(), MagicMock(), MagicMock(),
                                     MagicMock(), MagicMock())
    cb.answer.assert_awaited_once_with(
        "Стоп уже стоит (сделка #23) — закрыть можно в «Позиции».", show_alert=True
    )
    cb.message.answer.assert_not_awaited()


async def test_yes_close_records_result_and_resolves_alarm(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.execution.opening.service import ConfirmOutcome

    result = ConfirmOutcome("🚨 Закрыто кнопкой…", OpeningStatus.EMERGENCY_CLOSED, 27)
    opening = SimpleNamespace(id=15, symbol="XRP-USDT", side=TradeSide.LONG)
    service = _patch(monkeypatch, opening, result)
    edits = AsyncMock()
    resolved = AsyncMock()
    monkeypatch.setattr(outbox, "edit", edits)
    monkeypatch.setattr(opening_messages, "resolve_alarm", resolved)
    cb = _callback(f"{OpenCB.CLOSE_YES}15", message_id=530)
    await open_trade.close_alarm(cb, SimpleNamespace(id=1), MagicMock(), MagicMock(),
                                 MagicMock(), MagicMock(), MagicMock())
    service.close_alarm.assert_awaited_once_with(15)
    [call] = edits.await_args_list
    assert call.args[2].kind == "OPEN_EMERGENCY_CLOSED" and call.args[3] == "🚨 Закрыто кнопкой…"
    assert _labels(call.args[4]) == ["📋 К позициям", "◀️ В меню"]
    resolved.assert_awaited_once()
    assert resolved.await_args.args[2:] == (15, OpeningStatus.EMERGENCY_CLOSED)


async def test_yes_close_audit_failure_sends_nothing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    service = _patch(monkeypatch, None)
    monkeypatch.setattr(open_trade, "_audit", AsyncMock(return_value=False))
    cb = _callback(f"{OpenCB.CLOSE_YES}15")
    await open_trade.close_alarm(cb, MagicMock(), MagicMock(), MagicMock(), MagicMock(),
                                 MagicMock(), MagicMock())
    service.close_alarm.assert_not_awaited()
    cb.answer.assert_awaited_once_with(open_trade.AUDIT_FAILED_TEXT, show_alert=True)
