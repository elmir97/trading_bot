"""Числовой вопрос бота с ForceReply (03.10).

ask_number — отдельное короткое сообщение «✍️ …» с ForceReply и подсказкой в
поле ввода: шаг формы со своими inline-кнопками («Назад», «Пропустить», «В
меню») остаётся как был — ForceReply и inline-клавиатура в одном сообщении
несовместимы, а edit_message_text ForceReply не принимает. Клиент Telegram
держит ответ на вопрос, даже когда сверху пришли уведомления.

PromptMiddleware убирает вопрос, когда он больше не нужен, и задаёт его
заново, когда нужен снова:
- ответ принят, шаг сменился (другое состояние FSM) — вопрос удаляется;
- состояние сброшено (команда посреди формы, «Отмена», конец формы) —
  удаляется;
- ответ не принят (то же состояние, тот же вопрос) — вопрос задаётся заново:
  после ответа клиент снимает «ответ на», и без повтора ошибка ввода снова
  уводит пользователя гадать, куда писать;
- новый вопрос (ask_number в хендлере) — старый удалил сам ask_number, а если
  хендлер перед этим сбросил состояние (id старого потерян) — middleware.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ForceReply, Message, TelegramObject

from app.core.input_prompt import (
    PLACEHOLDER_LIMIT,
    PROMPT_AT_KEY,
    PROMPT_ID_KEY,
    PROMPT_KEYS,
    PROMPT_PLACEHOLDER_KEY,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

ASK_TEXT = "✍️ Ответь числом на это сообщение."
REPEAT_TEXT = "✍️ Не принял — ответь числом ещё раз на это сообщение."


# Вопросы, удаление которых уже пробовали в этом апдейте: middleware не
# удаляет второй раз то, что убрал ask_number (лишний «не удалён» в логе).
_attempted: ContextVar[set[int] | None] = ContextVar("prompt_delete_attempted", default=None)


async def _delete(bot: Bot, chat_id: int, message_id: int) -> None:
    attempted = _attempted.get()
    if attempted is not None:
        attempted.add(message_id)
    # 03.10.2026: исход удаления — в лог. Без него по логу не понять, ушёл ли
    # вопрос из чата (разбор проверки владельца 03.10, повтор после «Абв»).
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError as exc:
        # Уже удалён пользователем или старше 48 часов — работе не мешает.
        logger.info(
            "Вопрос с ForceReply не удалён",
            extra={"message_id": message_id, "error": type(exc).__name__},
        )
        return
    logger.info("Вопрос с ForceReply удалён", extra={"message_id": message_id})


def _target(event: Message | CallbackQuery) -> Message | None:
    if isinstance(event, CallbackQuery):
        return event.message if isinstance(event.message, Message) else None
    return event


async def ask_number(
    event: Message | CallbackQuery, state: FSMContext, placeholder: str,
    *, text: str = ASK_TEXT,
) -> None:
    """Вопрос с ForceReply и подсказкой (обрезается до 64 символов)."""
    message = _target(event)
    if message is None or message.bot is None:
        return
    bot, chat_id = message.bot, message.chat.id
    data = await state.get_data()
    old = data.get(PROMPT_ID_KEY)
    if old:
        await _delete(bot, chat_id, old)
    sent = await bot.send_message(
        chat_id, text,
        # selective не включать (03.10): в личном чате без @упоминания и без
        # ответа на сообщение он не нацелен ни на кого — клиент не открывал
        # «ответ на» и не показывал подсказку (проверка владельца на телефоне).
        reply_markup=ForceReply(
            force_reply=True, input_field_placeholder=placeholder[:PLACEHOLDER_LIMIT],
        ),
    )
    logger.info(
        "Вопрос с ForceReply отправлен",
        extra={"message_id": sent.message_id, "placeholder": placeholder[:PLACEHOLDER_LIMIT]},
    )
    await state.update_data({
        PROMPT_ID_KEY: sent.message_id,
        PROMPT_AT_KEY: datetime.now(UTC).isoformat(),
        PROMPT_PLACEHOLDER_KEY: placeholder,
    })


async def drop_prompt(bot: Bot, chat_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    prompt = data.get(PROMPT_ID_KEY)
    if prompt:
        await _delete(bot, chat_id, prompt)
    if any(k in data for k in PROMPT_KEYS):
        await state.set_data({k: v for k, v in data.items() if k not in PROMPT_KEYS})


class PromptMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        state: FSMContext | None = data.get("state")
        if state is None or not isinstance(event, (Message, CallbackQuery)):
            return await handler(event, data)
        before = await state.get_data()
        before_id = before.get(PROMPT_ID_KEY)
        before_state = await state.get_state()
        attempted: set[int] = set()
        token = _attempted.set(attempted)
        try:
            result = await handler(event, data)
        finally:
            _attempted.reset(token)
        if not before_id:
            return result
        message = _target(event)
        if message is None or message.bot is None:
            return result
        after = await state.get_data()
        after_id = after.get(PROMPT_ID_KEY)
        if after_id != before_id:
            # Состояние сброшено или задан новый вопрос — старый не нужен.
            # Новый вопрос после state.clear() (старт /risk, мастера) старый
            # id уже не видит, и до 03.10 вопрос оставался висеть в чате.
            if before_id not in attempted:
                await _delete(message.bot, message.chat.id, before_id)
            return result
        if await state.get_state() != before_state:
            await drop_prompt(message.bot, message.chat.id, state)
        elif isinstance(event, Message):
            logger.info(
                "Ввод не принят",
                extra={"state": before_state, "text": (getattr(event, "text", None) or "")[:16]},
            )
            await ask_number(
                event, state, after.get(PROMPT_PLACEHOLDER_KEY) or "Число", text=REPEAT_TEXT
            )
        return result
