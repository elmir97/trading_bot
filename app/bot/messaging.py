"""Общие операции над сообщениями бота."""

from __future__ import annotations

import contextlib

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message


async def edit_or_replace(
    message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> Message | None:
    """Заменяет содержимое сообщения текстом.

    У сообщения с фото нет текста, и edit_text падает («there is no text in
    the message to edit») — а «Назад»/«В меню» нажимают как раз под графиком.
    Тогда фото удаляется, а текст уходит новым сообщением. Ошибка удаления
    (например, сообщению больше 48 часов) не мешает показать экран.

    Возвращает новое сообщение, если текст ушёл новым, иначе None (правка
    на месте) — журналу исходящих нужен его message_id.
    """
    if message.photo:
        with contextlib.suppress(TelegramBadRequest):
            await message.delete()
        return await message.answer(text, reply_markup=reply_markup)
    await message.edit_text(text, reply_markup=reply_markup)
    return None
