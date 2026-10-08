"""Журнал исходящих итоговых сообщений (очередь A.2, 09.10.2026).

send() / edit() — отправка и правка итога с записью в outgoing_messages;
record() — запись уже отправленного или отредактированного сообщения. Все
три пишут INFO «Итоговое сообщение …» с message_id — сверка по логу и по
базе без скринов.

Своя сессия с немедленным коммитом (как app/execution/callback_audit.py):
сессия апдейта откатывается при падении хендлера, а запись нужна как раз
тогда. Сбой записи — ERROR в лог, наружу не пробрасывается: сообщение
пользователю уже ушло, журнал вторичен.

chat_id в лог не пишется: в личном чате он равен telegram_id.
"""

from __future__ import annotations

from datetime import UTC, datetime

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.bot.messaging import edit_or_replace
from app.core.logging import get_logger
from app.database.models.outgoing_message import OutgoingMessage, OutgoingMeta
from app.database.session import Database
from app.workers.notifier import Delivery, send_notification_message

logger = get_logger(__name__)


async def record(
    db: Database, meta: OutgoingMeta, *, chat_id: int, message_id: int, text: str
) -> None:
    """Новое сообщение — строка; то же (chat_id, message_id) — правка: прежний
    текст и вид уходят в edits, text и kind — текущие."""
    edited = False
    try:
        async with db.session() as session:
            row = await session.scalar(
                select(OutgoingMessage).where(
                    OutgoingMessage.chat_id == chat_id, OutgoingMessage.message_id == message_id
                ).with_for_update()
            )
            now = datetime.now(UTC)
            if row is None:
                session.add(OutgoingMessage(
                    user_id=meta.user_id, chat_id=chat_id, message_id=message_id,
                    kind=meta.kind, trade_opening_id=meta.trade_opening_id,
                    trade_id=meta.trade_id, position_action_id=meta.position_action_id,
                    text=text, sent_at=now,
                ))
            else:
                edited = True
                row.edits = [
                    *row.edits, {"at": now.isoformat(), "kind": row.kind, "text": row.text}
                ]
                row.text = text
                row.kind = meta.kind
                row.edited_at = now
                row.trade_opening_id = meta.trade_opening_id or row.trade_opening_id
                row.trade_id = meta.trade_id or row.trade_id
                row.position_action_id = meta.position_action_id or row.position_action_id
            await session.commit()
    except Exception:
        logger.exception(
            "Итоговое сообщение не записано в журнал",
            extra={"kind": meta.kind, "message_id": message_id, "user_id": meta.user_id},
        )
        return
    logger.info(
        "Итоговое сообщение исправлено" if edited else "Итоговое сообщение отправлено",
        extra={
            "kind": meta.kind, "message_id": message_id, "user_id": meta.user_id,
            "opening_id": meta.trade_opening_id, "trade_id": meta.trade_id,
            "position_action_id": meta.position_action_id,
        },
    )


async def send(
    bot: Bot, db: Database, meta: OutgoingMeta, chat_id: int, text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Delivery:
    """Новым сообщением (фоновые циклы: восстановление открытий, лимиты)."""
    delivery, sent = await send_notification_message(
        bot, chat_id, text, reply_markup=reply_markup
    )
    if sent is not None:
        await record(db, meta, chat_id=sent.chat.id, message_id=sent.message_id, text=text)
    return delivery


async def edit(
    message: Message, db: Database, meta: OutgoingMeta, text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message | None:
    """Правкой сообщения, под которым нажата кнопка (итог «Да» на карточке)."""
    shown = await edit_or_replace(message, text, reply_markup)
    target = shown or message
    await record(db, meta, chat_id=target.chat.id, message_id=target.message_id, text=text)
    return shown
