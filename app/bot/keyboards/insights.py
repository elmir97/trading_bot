"""Клавиатуры раздела разбора журнала."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


class InsightsCallback(CallbackData, prefix="ins"):
    action: str
    period: str = "month"
    force: bool = False


def periods_keyboard() -> InlineKeyboardMarkup:
    """Экран первого уровня (открывается прямо из меню) — только «В меню»."""
    builder = InlineKeyboardBuilder()
    for label, period in (("Неделя", "week"), ("Месяц", "month"), ("Квартал", "quarter")):
        builder.button(
            text=label,
            callback_data=InsightsCallback(action="analyze", period=period),
        )
    builder.button(text="◀️ В меню", callback_data="menu:main")
    builder.adjust(3, 1)
    return builder.as_markup()


def insights_menu() -> InlineKeyboardMarkup:
    """Результат разбора — второй уровень: «Назад» на выбор периода."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Другой период", callback_data="insights:menu")],
            [InlineKeyboardButton(text="◀️ В меню", callback_data="menu:main")],
        ]
    )
