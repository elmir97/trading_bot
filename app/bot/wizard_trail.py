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

10.10.2026 (A.1, находка Т2): мастер правит и удаляет только то, что
отправил сам в этом сценарии. Шаг по кнопке правит сообщение кнопки, только
если оно в переписке (owned()); иначе — новым сообщением: мастер, начатый с
кнопки под итогом, больше не переписывает и не удаляет итог. Итоговые
сообщения (журнал исходящих) не удаляются, даже если попали в переписку, —
WARNING и отметка в журнале. В лог уборки — список message_id.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.bot import outbox
from app.core.logging import get_logger
from app.database.session import Database

logger = get_logger(__name__)

TRAIL_KEY = "wizard_trail"
# Мастер журнала и мастер «Открыть на бирже» (05.10.2026) — одна переписка:
# переход между группами (выбор пути в начале) её не теряет.
WIZARD_PREFIXES = ("AddTradeStates:", "OpenTradeStates:")

_added: ContextVar[set[int] | None] = ContextVar("wizard_trail_added", default=None)
# Переписка на начало апдейта: хендлер может сбросить состояние (state.clear())
# до того, как покажет шаг, — сообщение остаётся своим.
_before: ContextVar[frozenset[int]] = ContextVar("wizard_trail_before", default=frozenset())


async def remember(state: FSMContext, message_id: int) -> None:
    data = await state.get_data()
    trail = list(data.get(TRAIL_KEY, []))
    if message_id not in trail:
        trail.append(message_id)
        await state.update_data({TRAIL_KEY: trail})
    added = _added.get()
    if added is not None:
        added.add(message_id)


async def owned(state: FSMContext, message_id: int) -> bool:
    """Сообщение отправлено мастером в этом сценарии (или это ответ
    пользователя ему) — его можно править как шаг мастера."""
    if message_id in _before.get() or message_id in (_added.get() or ()):
        return True
    return message_id in (await state.get_data()).get(TRAIL_KEY, [])


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
        before_token = _before.set(frozenset(before))
        try:
            result = await handler(event, data)
        finally:
            _before.reset(before_token)
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
            await _clean(target.bot, target.chat.id, sorted(lost), data.get("db"))
        return result


async def _clean(bot: Bot, chat_id: int, lost: list[int], db: Database | None) -> None:
    protected: set[int] = set()
    if db is not None:
        try:
            protected = await outbox.finals(db, chat_id, lost)
        except Exception:
            # Не знаем, какие из них итоговые, — не удаляем ничего.
            logger.exception(
                "Переписка мастера не удалена: журнал исходящих не прочитан",
                extra={"message_ids": lost},
            )
            return
    if protected and db is not None:
        logger.warning(
            "Итоговое сообщение в переписке мастера — не удаляю",
            extra={"message_ids": sorted(protected)},
        )
        await outbox.mark(db, chat_id, protected, "delete_skipped", "wizard_trail")
    deleted: list[int] = []
    failed: list[int] = []
    for message_id in lost:
        if message_id in protected:
            continue
        try:
            await bot.delete_message(chat_id, message_id)
            deleted.append(message_id)
        except TelegramAPIError:
            failed.append(message_id)   # уже удалено или старше 48 часов
    logger.info(
        "Переписка мастера удалена",
        extra={
            "deleted": len(deleted), "total": len(lost), "message_ids": deleted,
            "failed_ids": failed, "kept_final_ids": sorted(protected),
        },
    )
