"""Переписка мастера «Добавить сделку» убирается после записи или отмены
(03.10.2026, замечание владельца).

Сообщения мастера — шаги, ошибки ввода, «Плановый RR», «Расчёт позиции» и
ответы пользователя — копятся в данных FSM (TRAIL_KEY). Когда форма ушла из
AddTradeStates (сделка записана, «В меню», команда посреди формы) или
состояние сброшено, WizardTrailMiddleware удаляет их. Остаются карточка
сделки (новое сообщение или сообщение нажатой кнопки — его не трогаем) и
пояснение об отмене. Вопросы с ForceReply убирает PromptMiddleware.

remember() пишет id и в данные формы, и в набор текущего апдейта: после
state.clear() в том же хендлере данные пусты, а удалить надо всё.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.core.logging import get_logger

logger = get_logger(__name__)

TRAIL_KEY = "wizard_trail"
# Мастер журнала и мастер «Открыть на бирже» (05.10.2026) — одна переписка:
# переход между группами (выбор пути в начале) её не теряет.
WIZARD_PREFIXES = ("AddTradeStates:", "OpenTradeStates:")

_added: ContextVar[set[int] | None] = ContextVar("wizard_trail_added", default=None)


async def remember(state: FSMContext, message_id: int) -> None:
    data = await state.get_data()
    trail = list(data.get(TRAIL_KEY, []))
    if message_id not in trail:
        trail.append(message_id)
        await state.update_data({TRAIL_KEY: trail})
    added = _added.get()
    if added is not None:
        added.add(message_id)


def _in_wizard(state_name: str | None) -> bool:
    return bool(state_name) and str(state_name).startswith(WIZARD_PREFIXES)


class WizardTrailMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        state: FSMContext | None = data.get("state")
        if state is None or not isinstance(event, (Message, CallbackQuery)):
            return await handler(event, data)
        if not _in_wizard(await state.get_state()):
            return await handler(event, data)

        before = set((await state.get_data()).get(TRAIL_KEY, []))
        if isinstance(event, Message) and not (event.text or "").startswith("/"):
            # Ответ пользователя шагу мастера — тоже переписка мастера.
            await remember(state, event.message_id)
            before.add(event.message_id)
        added: set[int] = set()
        token = _added.set(added)
        try:
            result = await handler(event, data)
        finally:
            _added.reset(token)

        after = set((await state.get_data()).get(TRAIL_KEY, []))
        keep: set[int] = set()
        if isinstance(event, CallbackQuery) and isinstance(event.message, Message):
            # Сообщение нажатой кнопки хендлер отредактировал в итог
            # (карточка сделки, меню) — его не удаляем.
            keep.add(event.message.message_id)
        lost = (before | added) - after - keep
        target = event.message if isinstance(event, CallbackQuery) else event
        if lost and isinstance(target, Message) and target.bot is not None:
            deleted = 0
            for message_id in sorted(lost):
                try:
                    await target.bot.delete_message(target.chat.id, message_id)
                    deleted += 1
                except TelegramAPIError:
                    pass  # уже удалено или старше 48 часов
            logger.info(
                "Переписка мастера удалена",
                extra={"deleted": deleted, "total": len(lost)},
            )
        return result
