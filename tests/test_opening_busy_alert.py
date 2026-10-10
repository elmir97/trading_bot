"""Деплой 3, фикс 6: «Уже идёт действие с этой позицией» — всплывашкой на
нажатие, а не сообщением в чат. «Открываю…/Отменяю лимит…/Закрываю…» —
только после захвата лока (из сервиса, on_locked): на одно нажатие Telegram
принимает один ответ. Всплывашка не принята (срок ответа вышел за время
ожидания лока) — сообщением."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import CallbackQuery, Message

from app.bot import opening_messages, outbox
from app.bot.handlers import open_trade
from app.bot.handlers.open_trade import OpenCB
from app.execution.opening.service import BUSY_TEXT, ConfirmOutcome
from app.trading.enums import OpeningStatus, TradeSide

BUSY = ConfirmOutcome(BUSY_TEXT, final=False, busy=True)
OPENING = SimpleNamespace(id=15, status=OpeningStatus.ALARM, symbol="XRP-USDT",
                          side=TradeSide.LONG)


def _callback(data: str) -> MagicMock:
    message = MagicMock(spec=Message)
    message.bot = MagicMock()
    message.chat = SimpleNamespace(id=777)
    message.message_id = 530
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.message = message
    callback.answer = AsyncMock()
    return callback


def _service(monkeypatch, **methods: Any) -> MagicMock:  # type: ignore[no-untyped-def]
    service = MagicMock()
    service.load = AsyncMock(return_value=SimpleNamespace(
        id=15, status=OpeningStatus.WORKING, symbol="XRP-USDT", side=TradeSide.LONG,
    ))
    for name, value in methods.items():
        setattr(service, name, value)
    monkeypatch.setattr(open_trade, "_service", lambda *a, **k: service)
    monkeypatch.setattr(open_trade, "_audit", AsyncMock(return_value=True))
    monkeypatch.setattr(open_trade, "_is_current", AsyncMock(return_value=True))
    monkeypatch.setattr(outbox, "edit", AsyncMock())
    monkeypatch.setattr(opening_messages, "resolve_alarm", AsyncMock())
    return service


async def _close(cb: MagicMock) -> None:
    await open_trade.close_alarm(cb, SimpleNamespace(id=1), MagicMock(), MagicMock(),
                                 MagicMock(), MagicMock(), MagicMock())


async def test_close_busy_is_alert_not_message(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _service(monkeypatch, close_alarm=AsyncMock(return_value=BUSY))
    cb = _callback(f"{OpenCB.CLOSE_YES}15")
    await _close(cb)
    cb.answer.assert_awaited_once_with(BUSY_TEXT, show_alert=True)   # без «Закрываю…»
    cb.message.answer.assert_not_awaited()
    cb.message.edit_text.assert_not_awaited()
    outbox.edit.assert_not_awaited()   # type: ignore[attr-defined]


async def test_close_busy_alert_too_late_falls_back_to_message(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _service(monkeypatch, close_alarm=AsyncMock(return_value=BUSY))
    cb = _callback(f"{OpenCB.CLOSE_YES}15")
    cb.answer = AsyncMock(side_effect=TelegramBadRequest(
        AnswerCallbackQuery(callback_query_id="1"), "query is too old"
    ))
    await _close(cb)
    cb.message.answer.assert_awaited_once_with(BUSY_TEXT)


async def test_close_acks_only_after_lock(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """«Закрываю…» — из on_locked (лок взят), один ответ на нажатие."""
    order: list[str] = []

    async def close_alarm(opening_id: int, *, on_locked: Any) -> ConfirmOutcome:
        order.append("lock")
        await on_locked()
        order.append("closed")
        return ConfirmOutcome("🚨 Закрыто кнопкой…", OpeningStatus.EMERGENCY_CLOSED, 27)

    _service(monkeypatch, close_alarm=close_alarm)
    cb = _callback(f"{OpenCB.CLOSE_YES}15")
    cb.answer = AsyncMock(side_effect=lambda *a, **k: order.append(f"answer:{a[0]}"))
    await _close(cb)
    assert order == ["lock", "answer:Закрываю…", "closed"]
    cb.answer.assert_awaited_once()


async def test_close_not_locked_answers_once(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Тревоги уже нет (ответ до лока) — пустой ответ на нажатие, итог сообщением."""
    gone = ConfirmOutcome("Стоп уже стоит…", OpeningStatus.DONE, 27, final=False)
    _service(monkeypatch, close_alarm=AsyncMock(return_value=gone))
    cb = _callback(f"{OpenCB.CLOSE_YES}15")
    await _close(cb)
    cb.answer.assert_awaited_once_with()
    cb.message.edit_text.assert_awaited_once_with("Стоп уже стоит…")


async def test_confirm_busy_is_alert(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _service(monkeypatch, confirm=AsyncMock(return_value=BUSY))
    cb = _callback(f"{OpenCB.YES}15")
    await open_trade.confirm_open(cb, MagicMock(), SimpleNamespace(id=1), MagicMock(),
                                  MagicMock(), MagicMock(), MagicMock(), MagicMock())
    cb.answer.assert_awaited_once_with(BUSY_TEXT, show_alert=True)
    cb.message.answer.assert_not_awaited()


async def test_cancel_limit_busy_is_alert(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _service(monkeypatch, cancel_limit=AsyncMock(return_value=BUSY))
    cb = _callback(f"{OpenCB.CANCEL_LIMIT}15")
    await open_trade.cancel_limit(cb, SimpleNamespace(id=1), MagicMock(), MagicMock(),
                                  MagicMock(), MagicMock(), MagicMock())
    cb.answer.assert_awaited_once_with(BUSY_TEXT, show_alert=True)
    cb.message.answer.assert_not_awaited()
