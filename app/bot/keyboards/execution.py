"""Клавиатуры карточки подтверждения входа (этап 15.4, раздел 5 ТЗ)."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


class ExecutionCB:
    # Шаг 15.5.2а: + notification_id — снимок уведомления, не строка слота.
    OPEN = "exn:open:"       # кнопка под уведомлением о сигнале READY
    YES = "exn:yes:"
    NO = "exn:no:"
    EXPIRED = "exec:expired"  # кнопка-заглушка после TTL, ничего не делает
    # До 15.5.2а: + signal_id. Кнопки в уже отправленных сообщениях живут
    # вечно — отдельный хендлер отвечает «устарело» и ничего не исполняет.
    LEGACY_OPEN = "exec:open:"
    LEGACY_YES = "exec:yes:"
    LEGACY_NO = "exec:no:"


def open_trade_button(notification_id: int) -> InlineKeyboardMarkup:
    """Кнопка под уведомлением о сигнале — только для READY (раздел 5 ТЗ).
    Адресует снимок уведомления (шаг 15.5.2а): «exn:open:» + 10 цифр — 19
    байт при лимите callback_data Telegram в 64."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="⚡ Открыть сделку",
            callback_data=f"{ExecutionCB.OPEN}{notification_id}",
        )
    )
    return builder.as_markup()


def confirm_keyboard(notification_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Да, открыть", callback_data=f"{ExecutionCB.YES}{notification_id}"
        ),
        InlineKeyboardButton(
            text="❌ Нет", callback_data=f"{ExecutionCB.NO}{notification_id}"
        ),
    )
    return builder.as_markup()


def expired_keyboard() -> InlineKeyboardMarkup:
    """Раздел 5 ТЗ: по истечении TTL кнопки заменяются, нажатие ничего не делает."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="⌛ Подтверждение просрочено", callback_data=ExecutionCB.EXPIRED
        )
    )
    return builder.as_markup()
