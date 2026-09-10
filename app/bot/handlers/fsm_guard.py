"""Прерывание формы при вводе команды.

Проблема, которую это решает: пока пользователь заполняет форму (например
добавляет сделку), все текстовые сообщения перехватывают хендлеры этого
состояния. Введённая в этот момент команда `/stats` попадала в форму как
название инструмента, и бот создавал сделку по инструменту «/STATS».

Решение: перед всеми остальными роутерами ловим сообщения, начинающиеся
с «/», при активном состоянии FSM, и отменяем форму.

Почему не «отменить и сразу выполнить команду»: aiogram резолвит состояние
один раз на апдейт и кладёт в данные события, а хендлер получает копию этих
данных. Очистить состояние так, чтобы фильтры следующих роутеров увидели
это в том же апдейте, штатными средствами нельзя. Опираться на
недокументированное поведение ради экономии одного нажатия не стоит.

Исключение — `/start`: он и без того сбрасывает FSM сам (см.
`cmd_start`) и сразу показывает меню. Перехватывать его здесь означало бы
вместо этого ответить «отправь команду ещё раз» — то есть /start ломал
бы форму, но не открывал бы меню за одно нажатие, как от него ожидают.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot.keyboards.main import main_menu
from app.core.logging import get_logger

router = Router(name="fsm_guard")
logger = get_logger(__name__)


@router.message(StateFilter("*"), F.text.startswith("/"))
async def command_interrupts_form(message: Message, state: FSMContext) -> None:
    # Состояние проверяем внутри, а не фильтром: инвертированный
    # StateFilter(None) в декораторе не срабатывает, и сообщения тихо
    # проходили мимо guard прямо в форму.
    current = await state.get_state()
    if current is None:
        raise SkipHandler  # формы нет, вмешиваться не во что

    command = (message.text or "").split()[0]
    bare_command = command.split("@", 1)[0].lower()
    if bare_command == "/start":
        raise SkipHandler  # /start сбрасывает форму и открывает меню сам

    await state.clear()
    logger.info(
        "Форма прервана командой",
        extra={"state": current, "command": command},
    )

    await message.answer(
        f"Заполнение формы отменено — введена команда {command}.\n\n"
        f"Отправь её ещё раз, чтобы выполнить.",
        reply_markup=main_menu(),
    )
