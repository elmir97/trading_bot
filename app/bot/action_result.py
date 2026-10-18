"""Итог восстановления действия этапа 4 в чате (B.9, 19.10.2026).

Новым сообщением с «⬅️ К позициям» (журнал исходящих, вид ACTION_RECOVERED);
у карточки, на которой нажато «Да», снимаются кнопки — если она не итоговая
(итоговые сообщения не правятся, A.1). Без хендлеров: модуль импортирует
reconciler; расчёт и запись — app/execution/action_recovery.py.
"""

from __future__ import annotations

import contextlib

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import outbox
from app.bot.keyboards.main import MenuCallback
from app.core.logging import get_logger
from app.database.models.outgoing_message import OutgoingMeta
from app.database.session import Database
from app.execution.action_recovery import RecoveryNote

logger = get_logger(__name__)

KIND = "ACTION_RECOVERED"


def to_positions() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ К позициям", callback_data=MenuCallback.OPEN_POSITIONS)
    builder.button(text="◀️ В меню", callback_data=MenuCallback.MAIN)
    builder.adjust(2)
    return builder.as_markup()


async def send_action_recovered(bot: Bot, db: Database, note: RecoveryNote) -> None:
    card = note.card_message_id
    if card is not None:
        try:
            final = card in await outbox.finals(db, note.telegram_id, [card])
        except Exception:
            logger.exception("Карточка действия: журнал исходящих не прочитан — кнопки не трогаю",
                             extra={"action_id": note.action_id})
            final = True
        if not final:
            # Кнопки карточки вторичны: сообщение удалено или старше 48 ч —
            # «Да» на ней и так ответит «уже отправлено/обработана».
            with contextlib.suppress(TelegramAPIError):
                await bot.edit_message_reply_markup(
                    chat_id=note.telegram_id, message_id=card, reply_markup=None
                )
    await outbox.send(
        bot, db, OutgoingMeta(user_id=note.user_id, kind=KIND, position_action_id=note.action_id),
        note.telegram_id, note.text, reply_markup=to_positions(),
    )
