"""Общие операции над сообщениями бота.

10.10.2026 (A.1, баг №1): итоговое сообщение — то, что есть в журнале
исходящих (app/bot/outbox.py: «✅ Открыто», «🚨 Аварийное закрытие», ALARM,
«✅ Закрыто», уведомления сверки), — навигация не правит. FinalMessageMiddleware
(app/bot/middlewares/final_messages.py) отмечает сообщение нажатой кнопки,
edit_or_replace() под ним показывает экран новым сообщением. Править итог
можно только намеренно — outbox.edit (allow_final=True).
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar, Token

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message

from app.core.logging import get_logger

logger = get_logger(__name__)

# (chat_id, message_id) итогового сообщения, под которым нажата кнопка
# текущего апдейта; None — не итог (или апдейт не кнопка).
_final: ContextVar[tuple[int, int] | None] = ContextVar("final_message", default=None)

NOT_MODIFIED = "message is not modified"


def set_final(chat_id: int, message_id: int) -> Token[tuple[int, int] | None]:
    return _final.set((chat_id, message_id))


def reset_final(token: Token[tuple[int, int] | None]) -> None:
    _final.reset(token)


def is_final(message: Message) -> bool:
    """Сообщение — итог из журнала исходящих: правке навигацией не подлежит."""
    final = _final.get()
    return final is not None and final == (message.chat.id, message.message_id)


async def edit_or_replace(
    message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None,
    *, allow_final: bool = False,
) -> Message | None:
    """Заменяет содержимое сообщения текстом.

    У сообщения с фото нет текста, и edit_text падает («there is no text in
    the message to edit») — а «Назад»/«В меню» нажимают как раз под графиком.
    Тогда фото удаляется, а текст уходит новым сообщением. Ошибка удаления
    (например, сообщению больше 48 часов) не мешает показать экран.

    Итоговое сообщение (is_final) без allow_final не правится: экран — новым
    сообщением, итог остаётся как был. «message is not modified» (тот же
    экран ещё раз, C.1) — не ошибка.

    Возвращает новое сообщение, если текст ушёл новым, иначе None (правка
    на месте) — журналу исходящих нужен его message_id.
    """
    if not allow_final and is_final(message):
        sent = await message.answer(text, reply_markup=reply_markup)
        logger.info(
            "Итоговое сообщение не правлю — экран новым сообщением",
            extra={"message_id": message.message_id, "new_message_id": sent.message_id},
        )
        return sent
    if message.photo:
        with contextlib.suppress(TelegramBadRequest):
            await message.delete()
        return await message.answer(text, reply_markup=reply_markup)
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if NOT_MODIFIED not in str(exc):
            raise
        logger.debug("Экран не изменился", extra={"message_id": message.message_id})
    return None
