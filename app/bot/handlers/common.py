"""Стартовые команды и главное меню."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.formatting import fmt_num
from app.bot.handlers.positions import open_actions
from app.bot.keyboards.main import MenuCallback, back_to_main, main_menu
from app.bot.messaging import edit_or_replace
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.numfmt import fmt_rr
from app.core.security import SecretCipher
from app.database.models.trading_plan import TradingPlan
from app.database.models.user import User
from app.database.repositories.user import UserRepository
from app.execution.position_view import parse_start_param

router = Router(name="common")
logger = get_logger(__name__)

GREETING = (
    "<b>Торговый журнал</b>\n\n"
    "Веду учёт сделок, считаю статистику, анализирую ошибки "
    "и ищу сетапы по твоей методологии.\n\n"
    "Выбери раздел:"
)


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    user: User,
    state: FSMContext,
    command: CommandObject,
    session: AsyncSession,
    settings: Settings,
    cipher: SecretCipher,
) -> None:
    # Сброс FSM: если пользователь бросил заполнение формы на середине,
    # /start должен возвращать в чистое состояние, а не в середину диалога.
    await state.clear()
    # «⚙️ Действия в чате» из Mini App: t.me/<бот>?start=pos-XRP-USDT-L — экран
    # действий позиции. Любой другой параметр (испорченный, чужой) — только меню,
    # к бирже не ходим.
    ref = parse_start_param(command.args)
    # Вид параметра, не сам текст: в чате Telegram deep-link выглядит как голый
    # «/start», по логу видно, что пришло на самом деле (17.10).
    kind = "нет" if not command.args else ("pos" if ref is not None else "другой")
    logger.info("Команда /start", extra={"param": kind})
    if ref is not None:
        await open_actions(message, session, user, settings, cipher, *ref)
        return
    await message.answer(GREETING, reply_markup=main_menu())


@router.callback_query(F.data == MenuCallback.MAIN)
async def back_to_menu(
    callback: CallbackQuery, state: FSMContext
) -> None:
    await state.clear()
    if isinstance(callback.message, Message):
        await edit_or_replace(callback.message, GREETING, main_menu())
    await callback.answer()


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "<b>Команды</b>\n\n"
        "/start — главное меню\n"
        "/stats — статистика\n"
        "/trades — история сделок\n"
        "/open — позиции: биржа и журнал\n"
        "/trade — добавить сделку\n"
        "/risk — калькулятор позиции\n"
        "/today, /week, /month — отчёты\n"
        "/mistakes — анализ ошибок\n"
        "/analysis — анализ рынка\n"
        "/settings — настройки\n"
        "/ping — проверка базы данных\n\n"
        "Быстрее пользоваться кнопками из /start.",
        reply_markup=back_to_main(),
    )


@router.message(Command("plan"))
async def cmd_plan(message: Message, user: User, session) -> None:
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
        f"Минимальный RR: 1:{fmt_rr(plan.min_risk_reward)}\n"
        f"Максимальное плечо: {plan.max_leverage}x\n\n"
        f"Инструменты: {symbols}\n"
        f"Таймфреймы: {timeframes}",
        reply_markup=back_to_main(),
    )
