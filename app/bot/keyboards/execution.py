"""Клавиатуры карточки подтверждения входа (этап 15.4, раздел 5 ТЗ)."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


class ExecutionCB:
    OPEN = "exec:open:"      # + signal_id — кнопка под карточкой сигнала READY
    YES = "exec:yes:"        # + signal_id
    NO = "exec:no:"          # + signal_id
    EXPIRED = "exec:expired"  # кнопка-заглушка после TTL, ничего не делает


def open_trade_button(signal_id: int) -> InlineKeyboardMarkup:
    """Кнопка под уведомлением о сигнале — только для READY (раздел 5 ТЗ)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="⚡ Открыть сделку", callback_data=f"{ExecutionCB.OPEN}{signal_id}"
        )
    )
    return builder.as_markup()


def confirm_keyboard(signal_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Да, открыть", callback_data=f"{ExecutionCB.YES}{signal_id}"
        ),
        InlineKeyboardButton(text="❌ Нет", callback_data=f"{ExecutionCB.NO}{signal_id}"),
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
