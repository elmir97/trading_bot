"""Кнопки под уведомлениями о сигналах, отправленными до 02.10.2026.

Сигналы удалены, а старые сообщения в чате остались — их кнопки «⚡ Открыть
сделку», «Да», «Нет» (callback_data «exn:…») должны отвечать, а не молчать.
Ответ честный, кнопки снимаются с сообщения. Ничего не пишет в базу и на
биржу не ходит.
"""

from __future__ import annotations

import contextlib

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

router = Router(name="signals_retired")

# Префикс кнопок входа по сигналу (бывший app/bot/keyboards/execution.py).
RETIRED_PREFIX = "exn:"
RETIRED_TEXT = "Сигналы отключены — вход по ним больше недоступен."


@router.callback_query(F.data.startswith(RETIRED_PREFIX))
async def signal_button_retired(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        # Сообщение могло быть удалено или уже без кнопок — ответ важнее.
        with contextlib.suppress(TelegramAPIError):
            await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer(RETIRED_TEXT, show_alert=True)
