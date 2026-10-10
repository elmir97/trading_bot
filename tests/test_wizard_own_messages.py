"""A.1 (10.10.2026, находка Т2): мастер правит и удаляет только то, что
отправил сам в этом сценарии. Начатый с кнопки под итогом (503 в Т2) — шаг
новым сообщением, итог не изменён и после «Да» не удалён. Итоговое сообщение
(журнал исходящих) уборка не удаляет, даже если оно в переписке; в лог
уборки — список message_id."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message

from app.bot import outbox, wizard_trail
from app.bot.handlers import open_trade, trades
from app.bot.states.trade import AddTradeStates, OpenTradeStates
from app.bot.wizard_trail import TRAIL_KEY, WizardTrailMiddleware, remember

TG = 777
FINAL = 503


class FakeBot:
    def __init__(self) -> None:
        self.deleted: list[int] = []

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append(message_id)


class Chat:
    """Чат: выдаёт id новым сообщениям и помнит, какие правились."""

    def __init__(self) -> None:
        self.bot = FakeBot()
        self.next_id = 600
        self.edited: list[int] = []
        self.sent: list[int] = []

    def message(self, message_id: int, text: str = "") -> MagicMock:
        message = MagicMock(spec=Message)
        message.bot = self.bot
        message.chat = SimpleNamespace(id=TG)
        message.message_id = message_id
        message.text = text

        async def answer(*args: Any, **kwargs: Any) -> MagicMock:
            new_id = self.next_id
            self.next_id += 1
            self.sent.append(new_id)
            return self.message(new_id)

        async def edit_text(*args: Any, **kwargs: Any) -> None:
            self.edited.append(message_id)

        message.answer = AsyncMock(side_effect=answer)
        message.edit_text = AsyncMock(side_effect=edit_text)
        return message

    def callback(self, message_id: int, data: str = "") -> MagicMock:
        callback = MagicMock(spec=CallbackQuery)
        callback.message = self.message(message_id)
        callback.data = data
        callback.answer = AsyncMock()
        return callback


def _state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=TG, user_id=TG))


async def _run(state: FSMContext, event: Any, handler: Any, **data: Any) -> None:
    await WizardTrailMiddleware()(handler, event, {"state": state, **data})


async def test_wizard_started_under_final_keeps_it_after_yes() -> None:
    """Тест владельца: мастер с кнопки под итогом → после «Да» итог не
    изменён и не удалён. Первый шаг — новым сообщением; дальше мастер правит
    только свои сообщения; «Да» на карточке убирает переписку без итога."""
    chat, state = Chat(), _state()

    async def start(event: Any, data: Any) -> None:
        await open_trade.start_add_trade(event, state)

    await _run(state, chat.callback(FINAL, "menu:add_trade"), start)
    assert chat.edited == [] and chat.sent == [600]
    assert (await state.get_data())[TRAIL_KEY] == [600]

    async def pick(event: Any, data: Any) -> None:   # следующий шаг — по кнопке на 600
        await state.set_state(OpenTradeStates.symbol)
        await open_trade._send(event, state, "Монета:", None)

    await _run(state, chat.callback(600), pick)
    assert chat.edited == [600]

    async def reply(event: Any, data: Any) -> None:  # ответ текстом → новый шаг
        await open_trade._send(event, state, "Стоп:", None)

    await _run(state, chat.message(650, "XRP"), reply)
    await remember(state, 700)     # карточка — сообщение мастера

    async def yes(event: Any, data: Any) -> None:    # «Да»: форма закрыта, карточка — итог
        await state.clear()

    await _run(state, chat.callback(700), yes)
    assert FINAL not in chat.bot.deleted and FINAL not in chat.edited
    assert sorted(chat.bot.deleted) == [600, 601, 650]


async def test_back_to_mode_edits_own_message() -> None:
    """«Назад» к выбору пути: show_mode сбрасывает состояние, но сообщение
    кнопки — шаг мастера, его правят, а не шлют новое."""
    chat, state = Chat(), _state()
    await state.set_state(OpenTradeStates.symbol)
    await remember(state, 600)

    async def back(event: Any, data: Any) -> None:
        await open_trade.show_mode(event, state)

    await _run(state, chat.callback(600, f"{open_trade.OpenCB.BACK}mode"), back)
    assert chat.edited == [600] and chat.sent == []
    assert chat.bot.deleted == []


async def test_journal_wizard_step_under_foreign_message_is_new() -> None:
    chat, state = Chat(), _state()
    await state.set_state(AddTradeStates.symbol)

    async def step(event: Any, data: Any) -> None:
        await trades._send(event, "Инструмент:", None, state=state)

    await _run(state, chat.callback(FINAL), step)
    assert chat.edited == [] and chat.sent == [600]
    assert FINAL not in (await state.get_data())[TRAIL_KEY]


async def test_journal_wizard_step_edits_own_message() -> None:
    chat, state = Chat(), _state()
    await state.set_state(AddTradeStates.symbol)
    await remember(state, 600)

    async def step(event: Any, data: Any) -> None:
        await trades._send(event, "Направление:", None, state=state)

    await _run(state, chat.callback(600), step)
    assert chat.edited == [600] and chat.sent == []


async def test_final_in_trail_not_deleted_and_marked(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Итог попал в переписку (как 503 до фикса) — уборка его не удаляет:
    WARNING и отметка в журнале исходящих; в логе уборки — список id."""
    chat, state = Chat(), _state()
    await state.set_state(OpenTradeStates.confirm)
    for message_id in (FINAL, 601, 602):
        await remember(state, message_id)
    marks: list[tuple[int, list[int], str, str]] = []

    async def finals(db: Any, chat_id: int, ids: Any) -> set[int]:
        return {FINAL} & set(ids)

    async def mark(db: Any, chat_id: int, ids: Any, event: str, by: str) -> None:
        marks.append((chat_id, sorted(ids), event, by))

    monkeypatch.setattr(outbox, "finals", finals)
    monkeypatch.setattr(outbox, "mark", mark)

    async def yes(event: Any, data: Any) -> None:
        await state.clear()

    with caplog.at_level(logging.INFO, logger=wizard_trail.logger.name):
        await _run(state, chat.callback(700), yes, db=object())
    assert sorted(chat.bot.deleted) == [601, 602]
    assert marks == [(TG, [FINAL], "delete_skipped", "wizard_trail")]
    warning = next(r for r in caplog.records if r.levelno == logging.WARNING)
    assert warning.message_ids == [FINAL]  # type: ignore[attr-defined]
    info = next(r for r in caplog.records if r.getMessage() == "Переписка мастера удалена")
    assert info.message_ids == [601, 602]  # type: ignore[attr-defined]
    assert info.kept_final_ids == [FINAL]  # type: ignore[attr-defined]
    assert (info.deleted, info.total) == (2, 3)  # type: ignore[attr-defined]


async def test_journal_unreadable_deletes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Журнал исходящих не прочитан — неизвестно, что итоговое: не удаляем."""
    chat, state = Chat(), _state()
    await state.set_state(OpenTradeStates.confirm)
    await remember(state, 601)

    async def finals(db: Any, chat_id: int, ids: Any) -> set[int]:
        raise RuntimeError("db down")

    monkeypatch.setattr(outbox, "finals", finals)

    async def yes(event: Any, data: Any) -> None:
        await state.clear()

    await _run(state, chat.callback(700), yes, db=object())
    assert chat.bot.deleted == []


async def test_failed_delete_listed_in_log(caplog: pytest.LogCaptureFixture) -> None:
    chat, state = Chat(), _state()
    await state.set_state(OpenTradeStates.confirm)
    for message_id in (601, 602):
        await remember(state, message_id)
    from aiogram.exceptions import TelegramBadRequest

    async def delete(chat_id: int, message_id: int) -> None:
        if message_id == 602:
            raise TelegramBadRequest(method=MagicMock(), message="message to delete not found")
        chat.bot.deleted.append(message_id)

    chat.bot.delete_message = delete  # type: ignore[method-assign]

    async def yes(event: Any, data: Any) -> None:
        await state.clear()

    with caplog.at_level(logging.INFO, logger=wizard_trail.logger.name):
        await _run(state, chat.callback(700), yes)
    info = next(r for r in caplog.records if r.getMessage() == "Переписка мастера удалена")
    assert info.message_ids == [601] and info.failed_ids == [602]  # type: ignore[attr-defined]
