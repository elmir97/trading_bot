"""Релиз 03.10.2026: переписка мастера убирается после записи/отмены
(app/bot/wizard_trail.py) и отсечка журнала в reconciler (M4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message

from app.bot.states.trade import AddTradeStates
from app.bot.wizard_trail import TRAIL_KEY, WizardTrailMiddleware, remember
from app.workers.reconciler import _exits_after

TG = 777


class FakeBot:
    def __init__(self) -> None:
        self.deleted: list[int] = []

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append(message_id)


def _state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=TG, user_id=TG))


def _message(bot: FakeBot, message_id: int, text: str = "84590") -> MagicMock:
    message = MagicMock(spec=Message)
    message.bot = bot
    message.chat = SimpleNamespace(id=TG)
    message.message_id = message_id
    message.text = text
    message.answer = AsyncMock()
    return message


def _callback(bot: FakeBot, message_id: int) -> MagicMock:
    callback = MagicMock(spec=CallbackQuery)
    callback.message = _message(bot, message_id, "")
    return callback


async def _run(state: FSMContext, event: Any, handler) -> None:  # type: ignore[no-untyped-def]
    await WizardTrailMiddleware()(handler, event, {"state": state})


async def _wizard_with_trail(state: FSMContext, *ids: int) -> None:
    await state.set_state(AddTradeStates.entry_reason)
    for message_id in ids:
        await remember(state, message_id)


async def test_saved_trade_removes_wizard_messages_keeps_card() -> None:
    """Сделка записана текстом «причина входа»: шаги, ошибки, ответы
    пользователя и сам последний ответ удалены; карточка — новое сообщение,
    в переписку не входит."""
    bot, state = FakeBot(), _state()
    await _wizard_with_trail(state, 3, 4, 5, 8)

    async def save(event, data):  # type: ignore[no-untyped-def]
        await state.clear()                         # _save_trade
        await event.answer("✅ Сделка записана")    # карточка, id 21

    await _run(state, _message(bot, 20, "тестовый сетап"), save)
    assert sorted(bot.deleted) == [3, 4, 5, 8, 20]


async def test_menu_button_keeps_pressed_message() -> None:
    """«В меню» посреди формы: сообщение кнопки стало меню — остаётся."""
    bot, state = FakeBot(), _state()
    await _wizard_with_trail(state, 3, 4, 7)

    async def to_menu(event, data):  # type: ignore[no-untyped-def]
        await state.clear()

    await _run(state, _callback(bot, 7), to_menu)
    assert sorted(bot.deleted) == [3, 4]


async def test_command_mid_form_keeps_command_and_explanation() -> None:
    bot, state = FakeBot(), _state()
    await _wizard_with_trail(state, 3, 4)

    async def guard(event, data):  # type: ignore[no-untyped-def]
        await state.clear()
        await event.answer("Ввод сделки отменён")

    await _run(state, _message(bot, 9, "/stats"), guard)
    assert sorted(bot.deleted) == [3, 4]


async def test_next_step_keeps_trail() -> None:
    bot, state = FakeBot(), _state()
    await _wizard_with_trail(state, 3)

    async def next_step(event, data):  # type: ignore[no-untyped-def]
        await state.set_state(AddTradeStates.stop_loss)
        await remember(state, 11)

    await _run(state, _message(bot, 10), next_step)
    assert bot.deleted == []
    assert (await state.get_data())[TRAIL_KEY] == [3, 10, 11]


async def test_restart_from_menu_removes_previous_trail() -> None:
    """«Добавить сделку» посреди формы: state.clear() и новый шаг на
    сообщении кнопки — старая переписка удалена, новая начата."""
    bot, state = FakeBot(), _state()
    await _wizard_with_trail(state, 3, 4)

    async def restart(event, data):  # type: ignore[no-untyped-def]
        await state.clear()
        await state.set_state(AddTradeStates.symbol)
        await remember(state, 12)

    await _run(state, _callback(bot, 12), restart)
    assert sorted(bot.deleted) == [3, 4]


async def test_outside_wizard_nothing_deleted() -> None:
    bot, state = FakeBot(), _state()

    async def other(event, data):  # type: ignore[no-untyped-def]
        await event.answer("статистика")

    await _run(state, _message(bot, 5, "/stats"), other)
    assert bot.deleted == []


# --- reconciler: отсечка журнала (M4) ----------------------------------------------

CUTOFF = datetime(2026, 10, 3, 20, 38, 34, tzinfo=UTC)


def test_exits_after_respects_cutoff() -> None:
    assert _exits_after(None, None) is None
    assert _exits_after(None, CUTOFF) == CUTOFF                      # сделка бота
    early = CUTOFF - timedelta(hours=1)
    late = CUTOFF + timedelta(hours=1)
    assert _exits_after(early, CUTOFF) == CUTOFF
    assert _exits_after(late, CUTOFF) == late
    assert _exits_after(late, None) == late
