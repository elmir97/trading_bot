"""Клавиатуры главного меню.

Всё через InlineKeyboard: на телефоне тап быстрее и надёжнее ввода команд,
а callback_data даёт строгий набор допустимых действий вместо парсинга
свободного текста.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


class MenuCallback:
    """Префиксы callback_data. Константы, а не строки по месту вызова —
    опечатка тогда ловится при импорте, а не молча ломает кнопку."""

    MAIN = "menu:main"
    STATS = "menu:stats"
    TRADES = "menu:trades"
    ADD_TRADE = "menu:add_trade"
    OPEN_POSITIONS = "menu:open"
    ANALYSIS = "menu:analysis"
    FIND_ENTRY = "menu:find_entry"
    MISTAKES = "menu:mistakes"
    RISK = "menu:risk"
    DRAWDOWN = "menu:drawdown"
    REPORT = "menu:report"
    SETTINGS = "menu:settings"
    EXCHANGE = "menu:exchange"


def main_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📊 Статистика", callback_data=MenuCallback.STATS),
        InlineKeyboardButton(text="📝 Добавить сделку", callback_data=MenuCallback.ADD_TRADE),
    )
    builder.row(
        InlineKeyboardButton(text="💼 Открытые", callback_data=MenuCallback.OPEN_POSITIONS),
        InlineKeyboardButton(text="📈 Сделки", callback_data=MenuCallback.TRADES),
    )
    builder.row(
        InlineKeyboardButton(text="🔎 Анализ рынка", callback_data=MenuCallback.ANALYSIS),
        InlineKeyboardButton(text="🎯 Найти вход", callback_data=MenuCallback.FIND_ENTRY),
    )
    builder.row(
        InlineKeyboardButton(text="🧠 Анализ ошибок", callback_data="insights:menu"),
        InlineKeyboardButton(text="📉 Просадка", callback_data=MenuCallback.DRAWDOWN),
    )
    builder.row(
        InlineKeyboardButton(text="🧮 Риск", callback_data=MenuCallback.RISK),
        InlineKeyboardButton(text="📄 Отчёт", callback_data=MenuCallback.REPORT),
    )
    builder.row(
        InlineKeyboardButton(text="🏦 Биржа", callback_data=MenuCallback.EXCHANGE),
        InlineKeyboardButton(text="⚙️ Настройки", callback_data=MenuCallback.SETTINGS),
    )
    return builder.as_markup()


def back_to_main() -> InlineKeyboardMarkup:
    """Кнопка «В меню» для экранов первого уровня (открытых прямо из меню)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="◀️ В меню", callback_data=MenuCallback.MAIN)
    )
    return builder.as_markup()


def nav_row(parent_data: str, *, with_menu: bool = False) -> list[InlineKeyboardButton]:
    """Строка навигации для вложенного экрана: «Назад» на родителя.

    На экранах второго уровня одной «Назад» достаточно — до меню один тап
    через родителя. Начиная с третьего уровня рядом добавляется «В меню»,
    чтобы выйти в корень не пришлось листать через все шаги.
    """
    row = [InlineKeyboardButton(text="⬅️ Назад", callback_data=parent_data)]
    if with_menu:
        row.append(InlineKeyboardButton(text="◀️ В меню", callback_data=MenuCallback.MAIN))
    return row


def back_to(parent_data: str, *, with_menu: bool = False) -> InlineKeyboardMarkup:
    """Клавиатура из одной строки навигации — см. `nav_row`."""
    builder = InlineKeyboardBuilder()
    builder.row(*nav_row(parent_data, with_menu=with_menu))
    return builder.as_markup()


def with_menu_row(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Дописывает «В меню» к готовой клавиатуре экрана первого уровня."""
    rows = [*markup.inline_keyboard, [InlineKeyboardButton(text="◀️ В меню", callback_data=MenuCallback.MAIN)]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def with_nav(
    markup: InlineKeyboardMarkup, *, parent_data: str, with_menu: bool = False
) -> InlineKeyboardMarkup:
    """Добавляет строку навигации к уже собранной клавиатуре шага.

    Для экранов, где кроме «Назад» нужны содержательные кнопки (символы,
    стратегии, пропуск шага) — навигация дописывается последней строкой,
    а не встраивается в билдер каждого экрана по отдельности.
    """
    rows = [*markup.inline_keyboard, nav_row(parent_data, with_menu=with_menu)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_cancel(
    confirm_data: str, cancel_data: str = MenuCallback.MAIN
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=confirm_data),
        InlineKeyboardButton(text="❌ Отмена", callback_data=cancel_data),
    )
    return builder.as_markup()
