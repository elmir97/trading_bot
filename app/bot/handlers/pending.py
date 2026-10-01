"""Разделы меню, реализация которых приходится на следующие этапы.

Файл существует, чтобы кнопка не оставалась мёртвой: пользователь получает
честный ответ о статусе раздела вместо молчания. По мере готовности этапов
записи отсюда удаляются, а не остаются как постоянные заглушки.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards.main import MenuCallback, back_to_main

router = Router(name="pending_sections")

_PENDING: dict[str, str] = {
    MenuCallback.ANALYSIS: "Анализ рынка — этап 9, после подключения BingX.",
    # Кнопка «🎯 Найти вход» в меню прошлых сообщений: сигналы удалены
    # 02.10.2026, константы MenuCallback.FIND_ENTRY больше нет.
    "menu:find_entry": "Поиск входа убран: сигналы отключены. Техническая "
    "картина — в «Анализе рынка».",
}


@router.callback_query(F.data.in_(_PENDING))
async def pending_section(callback: CallbackQuery) -> None:
    text = _PENDING[str(callback.data)]
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"⏳ {text}", reply_markup=back_to_main()
        )
    await callback.answer()
