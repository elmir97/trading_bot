"""Стартовые команды и главное меню."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.formatting import fmt_num
from app.bot.keyboards.main import MenuCallback, back_to_main, main_menu
from app.database.models.trading_plan import TradingPlan
from app.database.models.user import User
from app.database.repositories.user import UserRepository

router = Router(name="common")

GREETING = (
    "<b>Торговый журнал</b>\n\n"
    "Веду учёт сделок, считаю статистику, анализирую ошибки "
    "и ищу сетапы по твоей методологии.\n\n"
    "Выбери раздел:"
)


@router.message(CommandStart())
async def cmd_start(message: Message, user: User, state: FSMContext) -> None:
    # Сброс FSM: если пользователь бросил заполнение формы на середине,
    # /start должен возвращать в чистое состояние, а не в середину диалога.
    await state.clear()
    await message.answer(GREETING, reply_markup=main_menu())


@router.callback_query(F.data == MenuCallback.MAIN)
async def back_to_menu(
    callback: CallbackQuery, state: FSMContext
) -> None:
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(GREETING, reply_markup=main_menu())
    await callback.answer()


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "<b>Команды</b>\n\n"
        "/start — главное меню\n"
        "/stats — статистика\n"
        "/trades — история сделок\n"
        "/open — открытые позиции\n"
        "/trade — добавить сделку\n"
        "/risk — калькулятор позиции\n"
        "/today, /week, /month — отчёты\n"
        "/mistakes — анализ ошибок\n"
        "/signal — поиск точки входа\n"
        "/settings — настройки\n"
        "/ping — проверка базы данных\n\n"
        "Быстрее пользоваться кнопками из /start.",
        reply_markup=back_to_main(),
    )


@router.message(Command("plan"))
async def cmd_plan(message: Message, user: User, session) -> None:  # noqa: ANN001
    repo = UserRepository(session)
    plan: TradingPlan | None = await repo.get_trading_plan(user.id)
    if plan is None:
        await message.answer("Торговый план не найден.", reply_markup=back_to_main())
        return

    symbols = ", ".join(plan.allowed_symbols) or "не ограничено"
    timeframes = ", ".join(plan.allowed_timeframes) or "не ограничено"
    await message.answer(
        "<b>Торговый план</b>\n\n"
        f"Риск на сделку: {fmt_num(plan.risk_per_trade_percent)}%\n"
        f"Дневной лимит убытка: {fmt_num(plan.max_daily_loss_percent)}%\n"
        f"Недельный лимит убытка: {fmt_num(plan.max_weekly_loss_percent)}%\n"
        f"Сделок в день: не более {plan.max_trades_per_day}\n"
        f"Минимальный RR: 1:{fmt_num(plan.min_risk_reward)}\n"
        f"Максимальное плечо: {plan.max_leverage}x\n\n"
        f"Инструменты: {symbols}\n"
        f"Таймфреймы: {timeframes}",
        reply_markup=back_to_main(),
    )
