"""Итог открытия сделки в чате — общий для чат-мастера, цикла открытий и
Mini App (docs/miniapp-plan.md §9а, подэтап 0): клавиатуры итога, снятие
кнопок со старой карточки, отправка итога новым сообщением.

Без хендлеров: модуль импортируют воркер и процесс API. callback_data
кнопок здесь — единственный источник; OpenCB и PositionsCB ссылаются сюда.
"""

from __future__ import annotations

import contextlib
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import outbox
from app.bot.keyboards.main import MenuCallback
from app.database.models.outgoing_message import OutgoingMeta
from app.database.session import Database
from app.execution.opening.service import ConfirmOutcome
from app.trading.enums import OpeningStatus, TradeSide
from app.workers.notifier import send_notification

POSITION_ACTIONS = "pos:act:"   # + SYMBOL:L|S — экран действий позиции (этап 4)
CANCEL_LIMIT = "ot:c:"          # + opening_id
CLOSE = "ot:x:"                 # + opening_id: «🔴 Закрыть маркетом» под ALARM
RECALC = "ot:recalc"
SIDE_CODE = {TradeSide.LONG: "L", TradeSide.SHORT: "S"}


def position_keyboard(symbol: str, side: TradeSide) -> InlineKeyboardMarkup:
    """Кнопка «Позиция» под итогом открытия сделки из бота (05.10.2026) —
    экран действий этой позиции (этап 4)."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text=f"⚙️ Позиция {symbol.replace('-USDT', '')} {side.value}",
        callback_data=f"{POSITION_ACTIONS}{symbol}:{SIDE_CODE[side]}",
    ))
    return builder.as_markup()


def closed_keyboard() -> InlineKeyboardMarkup:
    """Под итогом, после которого позиции нет (аварийное закрытие, A.1):
    «Позиция» вела бы на экран «уже нет»."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📋 К позициям", callback_data=MenuCallback.OPEN_POSITIONS),
        InlineKeyboardButton(text="◀️ В меню", callback_data=MenuCallback.MAIN),
    ]])


def opening_keyboard(
    status: OpeningStatus | None, opening_id: int | None,
    position: tuple[str, TradeSide] | None,
) -> InlineKeyboardMarkup | None:
    """Клавиатура итога открытия — одна для «Да» и для цикла восстановления."""
    if status is OpeningStatus.EMERGENCY_CLOSED:
        return closed_keyboard()
    if status is OpeningStatus.ALARM and opening_id is not None:
        # A.1: «⚙️ Позиция» и «🔴 Закрыть маркетом» (одно подтверждение).
        rows = list(position_keyboard(*position).inline_keyboard) if position else []
        return InlineKeyboardMarkup(inline_keyboard=[*rows, [InlineKeyboardButton(
            text="🔴 Закрыть маркетом", callback_data=f"{CLOSE}{opening_id}"
        )]])
    if position is not None:
        return position_keyboard(*position)
    if status is OpeningStatus.WORKING and opening_id is not None:
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="✖️ Отменить лимит", callback_data=f"{CANCEL_LIMIT}{opening_id}"
        )]])
    return None


_WITH_POSITION = (OpeningStatus.ALARM, OpeningStatus.PROTECTED)   # позиция есть, сделки ещё нет


def result_position(
    outcome: ConfirmOutcome, symbol: str, side: TradeSide
) -> tuple[str, TradeSide] | None:
    """Позиция для кнопки «⚙️ Позиция» под итогом «Открыть»: сделка записана
    или позиция уже на бирже (ALARM, PROTECTED)."""
    if outcome.trade_id is not None or outcome.status in _WITH_POSITION:
        return symbol, side
    return None


def result_keyboard(outcome: ConfirmOutcome, opening_id: int,
                    symbol: str, side: TradeSide) -> InlineKeyboardMarkup | None:
    if outcome.trade_id is not None or outcome.status in (OpeningStatus.WORKING, *_WITH_POSITION):
        return opening_keyboard(
            outcome.status, opening_id, result_position(outcome, symbol, side)
        )
    if outcome.status in (OpeningStatus.REFUSED, OpeningStatus.EXPIRED_CARD):
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Пересчитать", callback_data=RECALC)],
            [InlineKeyboardButton(text="◀️ В меню", callback_data=MenuCallback.MAIN)],
        ])
    return None


async def strip_markup(bot: Any, chat_id: int, message_id: int) -> None:
    """Снять кнопки с сообщения; уже снятые / удалённые / старше 48 ч — не ошибка."""
    with contextlib.suppress(TelegramAPIError):
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None
        )


async def send_opening_result(
    bot: Bot, db: Database, telegram_id: int, text: str, *,
    status: OpeningStatus | None, opening_id: int | None,
    position: tuple[str, TradeSide] | None, meta: OutgoingMeta | None = None,
) -> None:
    """Итог открытия новым сообщением с клавиатурой по статусу; с meta — через
    журнал исходящих (A.2)."""
    markup = opening_keyboard(status, opening_id, position)
    if meta is None:
        await send_notification(bot, telegram_id, text, reply_markup=markup)
        return
    await outbox.send(bot, db, meta, telegram_id, text, markup)


__all__ = [
    "CANCEL_LIMIT",
    "CLOSE",
    "POSITION_ACTIONS",
    "RECALC",
    "closed_keyboard",
    "opening_keyboard",
    "position_keyboard",
    "result_keyboard",
    "result_position",
    "send_opening_result",
    "strip_markup",
]
