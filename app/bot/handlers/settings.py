"""Калькулятор позиции, торговый план, настройки, биржевые ключи.

Калькулятор намеренно отделён от формы добавления сделки: чаще всего
размер позиции считают до входа, когда записывать в журнал ещё нечего.
"""

from __future__ import annotations

from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.main import MenuCallback, back_to, back_to_main, nav_row
from app.bot.keyboards.trade import (
    TradeCB,
    fmt_amount,
    fmt_money,
    fmt_num,
    fmt_qty,
    fmt_price,
    side_keyboard,
)
from app.bot.states.trade import RiskCalculatorStates, SettingsStates
from app.core.config import Settings
from app.core.security import SecretCipher, mask_secret
from app.database.models.credentials import ExchangeCredentials
from app.database.models.user import DEFAULT_NOTIFICATIONS, User
from app.database.repositories.user import UserRepository
from app.exchanges.base import ExchangeAuthError
from app.services.exchange_factory import ExchangeFactory
from app.services.permissions import refresh_permissions
from app.trading.calculations import (
    CalculationError,
    calculate_position_size,
    calculate_risk_reward,
    to_decimal,
)
from app.trading.enums import ExchangeKeyMode, TradeSide
from app.workers.notifier import NOTIFICATION_LABELS
from sqlalchemy import select

router = Router(name="settings")


class SetCB:
    RISK = "set:risk"
    DAILY = "set:daily"
    TRADES = "set:trades"
    NOTIFY = "set:notify:"
    NOTIFICATIONS = "set:notifications"
    PLAN = "set:plan"
    # Этап 15.4в: переключатель счёта (что показывать) и ключи по режимам.
    MODE = "set:mode"
    API = "set:api"                    # submenu: обе пары ключей разом
    API_MODE = "set:api:"              # + LIVE/DEMO — начать ввод пары
    API_MODE_DELETE = "set:api_delete:"  # + LIVE/DEMO — удалить пару
    API_MODE_BACK = "set:api_back:"    # + LIVE/DEMO — назад с шага secret
    API_MODE_CHECK = "set:api_check:"  # + LIVE/DEMO — принудительно проверить права (раздел 8 ТЗ)


async def _reply(event: Message | CallbackQuery, text: str, keyboard=None) -> None:  # type: ignore[no-untyped-def]
    if isinstance(event, CallbackQuery):
        if isinstance(event.message, Message):
            await event.message.edit_text(text, reply_markup=keyboard)
        await event.answer()
    else:
        await event.answer(text, reply_markup=keyboard)


# ---------------------------------------------------------------------------
# Калькулятор позиции
# ---------------------------------------------------------------------------


RISK_BACK = "risk:back"


async def _show_balance_prompt(
    event: Message | CallbackQuery, state: FSMContext, risk: Decimal
) -> None:
    await state.set_state(RiskCalculatorStates.balance)
    await state.update_data(risk_percent=str(risk))
    await _reply(
        event,
        "<b>Расчёт размера позиции</b>\n\n"
        f"Риск по плану: {fmt_num(risk)}%\n\n"
        "Введи баланс депозита в USDT:",
        back_to_main(),
    )


async def _show_risk_percent_prompt(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(RiskCalculatorStates.risk_percent)
    data = await state.get_data()
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"По плану: {fmt_num(Decimal(data['risk_percent']))}%", callback_data=TradeCB.SKIP
    )
    builder.adjust(1)
    builder.row(*nav_row(RISK_BACK))
    await _reply(
        event, "Риск в процентах или оставь значение из плана:", builder.as_markup()
    )


async def _show_entry_price_prompt(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(RiskCalculatorStates.entry_price)
    await _reply(event, "Цена входа:", back_to(RISK_BACK, with_menu=True))


async def _show_stop_loss_prompt(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(RiskCalculatorStates.stop_loss)
    await _reply(
        event,
        "Стоп-лосс:\n\n<i>Направление определю по тому, выше он или ниже входа.</i>",
        back_to(RISK_BACK, with_menu=True),
    )


@router.callback_query(F.data == MenuCallback.RISK)
@router.message(Command("risk"))
async def start_calculator(
    event: Message | CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    await state.clear()
    plan = await UserRepository(session).get_trading_plan(user.id)
    risk = plan.risk_per_trade_percent if plan else Decimal("2")
    await _show_balance_prompt(event, state, risk)


@router.message(RiskCalculatorStates.balance)
async def calc_balance(message: Message, state: FSMContext) -> None:
    try:
        balance = to_decimal(message.text or "", "баланс")
    except CalculationError:
        await message.answer("Введи число, например 10000")
        return
    if balance <= 0:
        await message.answer("Баланс должен быть больше нуля.")
        return

    await state.update_data(balance=str(balance))
    await _show_risk_percent_prompt(message, state)


@router.callback_query(RiskCalculatorStates.risk_percent, F.data == RISK_BACK)
async def risk_back_to_balance(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await _show_balance_prompt(callback, state, Decimal(data["risk_percent"]))
    await callback.answer()


@router.callback_query(RiskCalculatorStates.risk_percent, F.data == TradeCB.SKIP)
async def calc_keep_risk(callback: CallbackQuery, state: FSMContext) -> None:
    await _show_entry_price_prompt(callback, state)
    await callback.answer()


@router.message(RiskCalculatorStates.risk_percent)
async def calc_risk(message: Message, state: FSMContext) -> None:
    try:
        risk = to_decimal((message.text or "").rstrip("%"), "риск")
    except CalculationError:
        await message.answer("Введи число, например 2")
        return
    if risk <= 0 or risk > 100:
        await message.answer("Риск должен быть в диапазоне от 0 до 100.")
        return

    await state.update_data(risk_percent=str(risk))
    await _show_entry_price_prompt(message, state)


@router.callback_query(RiskCalculatorStates.entry_price, F.data == RISK_BACK)
async def risk_back_to_risk_percent(callback: CallbackQuery, state: FSMContext) -> None:
    await _show_risk_percent_prompt(callback, state)
    await callback.answer()


@router.message(RiskCalculatorStates.entry_price)
async def calc_entry(message: Message, state: FSMContext) -> None:
    try:
        entry = to_decimal(message.text or "", "цену входа")
    except CalculationError:
        await message.answer("Введи число.")
        return

    await state.update_data(entry_price=str(entry))
    await _show_stop_loss_prompt(message, state)


@router.callback_query(RiskCalculatorStates.stop_loss, F.data == RISK_BACK)
async def risk_back_to_entry_price(callback: CallbackQuery, state: FSMContext) -> None:
    await _show_entry_price_prompt(callback, state)
    await callback.answer()


@router.message(RiskCalculatorStates.stop_loss)
async def calc_stop(message: Message, state: FSMContext) -> None:
    try:
        stop = to_decimal(message.text or "", "стоп-лосс")
    except CalculationError:
        await message.answer("Введи число.")
        return

    data = await state.get_data()
    entry = Decimal(data["entry_price"])
    if stop == entry:
        await message.answer("Стоп не может совпадать с ценой входа.")
        return

    # Направление выводится из положения стопа: спрашивать отдельно
    # значит добавить шаг, который однозначно вычисляется.
    side = TradeSide.LONG if stop < entry else TradeSide.SHORT

    try:
        sizing = calculate_position_size(
            account_balance=Decimal(data["balance"]),
            risk_percent=Decimal(data["risk_percent"]),
            entry_price=entry,
            stop_loss=stop,
            side=side,
        )
    except CalculationError as exc:
        await message.answer(f"⚠️ {exc}")
        return

    await state.update_data(stop_loss=str(stop), side=side.value)
    await state.set_state(RiskCalculatorStates.take_profit)

    builder = InlineKeyboardBuilder()
    builder.button(text="Без цели", callback_data=TradeCB.SKIP)
    builder.adjust(1)
    builder.row(*nav_row(RISK_BACK, with_menu=True))

    await message.answer(
        f"<b>{side.label}</b>\n\n"
        f"Сумма риска: {fmt_amount(sizing.risk_amount)} USDT\n"
        f"Дистанция до стопа: {fmt_num(sizing.stop_distance_percent)}%\n\n"
        f"<b>Объём: {fmt_qty(sizing.quantity)}</b>\n"
        f"Размер позиции: {fmt_amount(sizing.position_value)} USDT\n\n"
        f"Маржа при плече:\n"
        f"1x — {fmt_amount(sizing.required_margin(1))}\n"
        f"5x — {fmt_amount(sizing.required_margin(5))}\n"
        f"10x — {fmt_amount(sizing.required_margin(10))}\n"
        f"20x — {fmt_amount(sizing.required_margin(20))}\n\n"
        f"Введи take-profit, чтобы посчитать RR и потенциальную прибыль:",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(RiskCalculatorStates.take_profit, F.data == RISK_BACK)
async def risk_back_to_stop_loss(callback: CallbackQuery, state: FSMContext) -> None:
    await _show_stop_loss_prompt(callback, state)
    await callback.answer()


@router.callback_query(RiskCalculatorStates.take_profit, F.data == TradeCB.SKIP)
async def calc_no_target(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer("Расчёт завершён.", reply_markup=back_to_main())


@router.message(RiskCalculatorStates.take_profit)
async def calc_target(message: Message, state: FSMContext) -> None:
    try:
        target = to_decimal(message.text or "", "take-profit")
    except CalculationError:
        await message.answer("Введи число.")
        return

    data = await state.get_data()
    await state.clear()

    side = TradeSide(data["side"])
    entry = Decimal(data["entry_price"])
    stop = Decimal(data["stop_loss"])

    try:
        rr = calculate_risk_reward(
            entry_price=entry, stop_loss=stop, take_profit=target, side=side
        )
        sizing = calculate_position_size(
            account_balance=Decimal(data["balance"]),
            risk_percent=Decimal(data["risk_percent"]),
            entry_price=entry,
            stop_loss=stop,
            side=side,
        )
    except CalculationError as exc:
        await message.answer(f"⚠️ {exc}", reply_markup=back_to_main())
        return

    potential_profit = sizing.risk_amount * rr
    verdict = (
        "✅ RR соответствует методологии (минимум 1:2)."
        if rr >= 2
        else "⚠️ RR ниже 1:2 — методология такой вход не одобряет."
    )

    await message.answer(
        f"<b>Сценарий сделки</b>\n\n"
        f"{side.label}\n"
        f"Вход: {fmt_price(entry)}\n"
        f"Стоп: {fmt_price(stop)}\n"
        f"Цель: {fmt_price(target)}\n\n"
        f"Объём: {fmt_qty(sizing.quantity)}\n"
        f"Риск: −{fmt_num(sizing.risk_amount)} USDT\n"
        f"Потенциал: +{fmt_num(potential_profit.quantize(Decimal('0.01')))} USDT\n"
        f"<b>RR: 1:{fmt_num(rr)}</b>\n\n"
        f"{verdict}",
        reply_markup=back_to_main(),
    )


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------


def settings_keyboard(
    active_mode: ExchangeKeyMode, has_live: bool, has_demo: bool
) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🧮 Риск на сделку", callback_data=SetCB.RISK),
        InlineKeyboardButton(text="📉 Дневной лимит", callback_data=SetCB.DAILY),
    )
    builder.row(
        InlineKeyboardButton(text="🔢 Сделок в день", callback_data=SetCB.TRADES),
        InlineKeyboardButton(text="📋 Торговый план", callback_data=SetCB.PLAN),
    )
    builder.row(
        InlineKeyboardButton(
            text=f"💱 Счёт: {active_mode.label}", callback_data=SetCB.MODE
        )
    )
    has_any = has_live or has_demo
    builder.row(
        InlineKeyboardButton(
            text="🔑 Ключи BingX" if not has_any else "🔑 Ключи (подключены)",
            callback_data=SetCB.API,
        )
    )
    builder.row(
        InlineKeyboardButton(text="🔔 Уведомления", callback_data=SetCB.NOTIFICATIONS)
    )
    builder.row(InlineKeyboardButton(text="◀️ В меню", callback_data=MenuCallback.MAIN))
    return builder


async def _get_credentials(
    session: AsyncSession, user_id: int, mode: ExchangeKeyMode
) -> ExchangeCredentials | None:
    return await session.scalar(
        select(ExchangeCredentials).where(
            ExchangeCredentials.user_id == user_id,
            ExchangeCredentials.exchange == "bingx",
            ExchangeCredentials.mode == mode,
        )
    )


@router.callback_query(F.data == MenuCallback.SETTINGS)
@router.message(Command("settings"))
async def show_settings(
    event: Message | CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    await state.clear()
    repo = UserRepository(session)
    plan = await repo.get_trading_plan(user.id)
    user_settings = await repo.get_settings(user.id)
    active_mode = user_settings.active_exchange_mode if user_settings else ExchangeKeyMode.LIVE
    live = await _get_credentials(session, user.id, ExchangeKeyMode.LIVE)
    demo = await _get_credentials(session, user.id, ExchangeKeyMode.DEMO)

    lines = ["<b>Настройки</b>", ""]
    if plan is not None:
        lines += [
            f"Риск на сделку: {fmt_num(plan.risk_per_trade_percent)}%",
            f"Дневной лимит убытка: {fmt_num(plan.max_daily_loss_percent)}%",
            f"Недельный лимит: {fmt_num(plan.max_weekly_loss_percent)}%",
            f"Сделок в день: {plan.max_trades_per_day}",
            f"Минимальный RR: 1:{fmt_num(plan.min_risk_reward)}",
        ]
    if user_settings is not None:
        lines.append(f"Часовой пояс: {user_settings.timezone}")

    lines.append("")
    lines.append(f"Счёт: {active_mode.label}")
    lines.append(
        f"Ключи · реальный: {live.api_key_masked if live else 'не подключены'}"
    )
    lines.append(
        f"Ключи · демо: {demo.api_key_masked if demo else 'не подключены'}"
    )

    await _reply(
        event,
        "\n".join(lines),
        settings_keyboard(active_mode, live is not None, demo is not None).as_markup(),
    )


@router.callback_query(F.data == SetCB.MODE)
async def toggle_exchange_mode(
    callback: CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    """Переключатель «Счёт: реальный/демо» (этап 15.4в).

    Меняет только то, какой счёт бот читает и показывает (баланс, позиции,
    цифры карточки подтверждения) — куда реально уходят ордера, решает
    конфиг (BINGX_TRADING_MODE), см. guards.check_mode_allowed."""
    settings_row = await UserRepository(session).get_settings(user.id)
    if settings_row is None:
        await callback.answer()
        return
    settings_row.active_exchange_mode = (
        ExchangeKeyMode.DEMO
        if settings_row.active_exchange_mode is ExchangeKeyMode.LIVE
        else ExchangeKeyMode.LIVE
    )
    await session.flush()
    await show_settings(callback, state, user, session)


@router.callback_query(F.data == SetCB.PLAN)
async def show_plan(callback: CallbackQuery, user: User, session: AsyncSession) -> None:
    back = back_to(MenuCallback.SETTINGS)
    plan = await UserRepository(session).get_trading_plan(user.id)
    if plan is None:
        await _reply(callback, "Торговый план не найден.", back)
        return

    await _reply(
        callback,
        "<b>Торговый план</b>\n\n"
        f"Риск на сделку: {fmt_num(plan.risk_per_trade_percent)}%\n"
        f"Дневной лимит убытка: {fmt_num(plan.max_daily_loss_percent)}%\n"
        f"Недельный лимит убытка: {fmt_num(plan.max_weekly_loss_percent)}%\n"
        f"Сделок в день: не более {plan.max_trades_per_day}\n"
        f"Минимальный RR: 1:{fmt_num(plan.min_risk_reward)}\n"
        f"Максимальное плечо: {plan.max_leverage}x\n\n"
        f"Инструменты: {', '.join(plan.allowed_symbols) or 'не ограничено'}\n"
        f"Таймфреймы: {', '.join(plan.allowed_timeframes) or 'не ограничено'}",
        back,
    )


@router.callback_query(F.data == SetCB.RISK)
async def ask_risk(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.risk_per_trade)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "Риск на сделку в процентах.\n\n"
            "<i>Методология держит 1–2% как потолок. При 2% три убытка "
            "подряд дают −6% депозита.</i>",
            reply_markup=back_to(MenuCallback.SETTINGS),
        )
    await callback.answer()


@router.message(SettingsStates.risk_per_trade)
async def set_risk(
    message: Message, state: FSMContext, user: User, session: AsyncSession
) -> None:
    try:
        value = to_decimal((message.text or "").rstrip("%"), "риск")
    except CalculationError:
        await message.answer("Введи число, например 2")
        return
    if value <= 0 or value > 100:
        await message.answer("Риск должен быть в диапазоне от 0 до 100.")
        return

    await state.clear()
    plan = await UserRepository(session).get_trading_plan(user.id)
    if plan is None:
        await message.answer(
            "Торговый план не найден.", reply_markup=back_to(MenuCallback.SETTINGS)
        )
        return

    plan.risk_per_trade_percent = value
    note = ""
    if value > 2:
        note = (
            f"\n\n⚠️ {fmt_num(value)}% выше потолка твоей методологии. "
            f"Три убытка подряд дадут −{fmt_num(value * 3)}% депозита."
        )

    await message.answer(
        f"Риск на сделку: {fmt_num(value)}%{note}",
        reply_markup=back_to(MenuCallback.SETTINGS),
    )


@router.callback_query(F.data == SetCB.DAILY)
async def ask_daily(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.max_daily_loss)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "Дневной лимит убытка в процентах:",
            reply_markup=back_to(MenuCallback.SETTINGS),
        )
    await callback.answer()


@router.message(SettingsStates.max_daily_loss)
async def set_daily(
    message: Message, state: FSMContext, user: User, session: AsyncSession
) -> None:
    try:
        value = to_decimal((message.text or "").rstrip("%"), "лимит")
    except CalculationError:
        await message.answer("Введи число, например 6")
        return
    if value <= 0 or value > 100:
        await message.answer("Значение должно быть в диапазоне от 0 до 100.")
        return

    await state.clear()
    plan = await UserRepository(session).get_trading_plan(user.id)
    if plan is not None:
        plan.max_daily_loss_percent = value
    await message.answer(
        f"Дневной лимит убытка: {fmt_num(value)}%",
        reply_markup=back_to(MenuCallback.SETTINGS),
    )


@router.callback_query(F.data == SetCB.TRADES)
async def ask_trades(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.max_trades_per_day)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "Максимум сделок в день:", reply_markup=back_to(MenuCallback.SETTINGS)
        )
    await callback.answer()


@router.message(SettingsStates.max_trades_per_day)
async def set_trades(
    message: Message, state: FSMContext, user: User, session: AsyncSession
) -> None:
    try:
        value = int((message.text or "").strip())
    except ValueError:
        await message.answer("Введи целое число, например 5")
        return
    if value < 1:
        await message.answer("Значение должно быть не меньше 1.")
        return

    await state.clear()
    plan = await UserRepository(session).get_trading_plan(user.id)
    if plan is not None:
        plan.max_trades_per_day = value
    await message.answer(
        f"Максимум сделок в день: {value}", reply_markup=back_to(MenuCallback.SETTINGS)
    )


# ---------------------------------------------------------------------------
# Биржевые ключи (этап 15.4в: отдельная пара на LIVE и на DEMO)
# ---------------------------------------------------------------------------


def _api_keys_menu(
    live: ExchangeCredentials | None, demo: ExchangeCredentials | None
) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for mode, creds in ((ExchangeKeyMode.LIVE, live), (ExchangeKeyMode.DEMO, demo)):
        label = mode.label
        builder.row(
            InlineKeyboardButton(
                text=f"✏️ {label}: заменить" if creds else f"➕ {label}: добавить",
                callback_data=f"{SetCB.API_MODE}{mode.value}",
            )
        )
        if creds is not None:
            builder.row(
                InlineKeyboardButton(
                    text=f"🔍 Проверить права · {label}",
                    callback_data=f"{SetCB.API_MODE_CHECK}{mode.value}",
                )
            )
            builder.row(
                InlineKeyboardButton(
                    text=f"🗑 Удалить · {label}",
                    callback_data=f"{SetCB.API_MODE_DELETE}{mode.value}",
                )
            )
    builder.row(*nav_row(MenuCallback.SETTINGS))
    return builder


async def _show_api_keys_menu(
    event: Message | CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    await state.clear()
    live = await _get_credentials(session, user.id, ExchangeKeyMode.LIVE)
    demo = await _get_credentials(session, user.id, ExchangeKeyMode.DEMO)

    lines = [
        "<b>Ключи BingX</b>",
        "",
        "Каждый счёт — отдельная пара ключей, обе шифруются перед записью "
        "в базу и никогда не попадают в логи.",
        "",
        f"{ExchangeKeyMode.LIVE.label}: "
        f"{live.api_key_masked if live else 'не подключены'}",
        f"{ExchangeKeyMode.DEMO.label}: "
        f"{demo.api_key_masked if demo else 'не подключены'}",
    ]
    await _reply(event, "\n".join(lines), _api_keys_menu(live, demo).as_markup())


@router.callback_query(F.data == SetCB.API)
async def show_api_keys(
    callback: CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    await _show_api_keys_menu(callback, state, user, session)


_KEY_REQUIREMENTS = {
    ExchangeKeyMode.DEMO: (
        "Ключ создаётся в личном кабинете BingX в режиме <b>демо-торговли</b> "
        "(виртуальные USDT, VST). Нужны права <b>Read + Perpetual Futures "
        "Trading</b> — без них бот не сможет отправлять ордера на демо-счёт. "
        "Вывод средств (Withdraw) — выключить."
    ),
    ExchangeKeyMode.LIVE: (
        "Права <b>Perpetual Futures Trading</b> понадобятся только начиная "
        "с этапа 15.7 — пока что достаточно ключа с правом <b>Read</b>. "
        "Вывод средств (Withdraw) — выключить в любом случае."
    ),
}


async def _show_api_key_prompt(
    event: Message | CallbackQuery,
    state: FSMContext,
    user: User,
    session: AsyncSession,
    mode: ExchangeKeyMode,
) -> None:
    """Экран ввода API Key для конкретного режима — общий для первого
    захода и для «Назад» с шага Secret. Требования к правам ключа зависят
    от режима (этап 15.4в): DEMO сразу торгует на VST и без прав на
    фьючерсы бесполезен, LIVE до этапа 15.7 участвует только чтением."""
    creds = await _get_credentials(session, user.id, mode)

    await state.set_state(SettingsStates.api_key)
    await state.update_data(mode=mode.value)
    text = (
        f"<b>Подключение BingX — {mode.label}</b>\n\n"
        f"{_KEY_REQUIREMENTS[mode]}\n\n"
        "IP whitelist: <code>147.45.111.10</code> — обязательно, без исключений.\n\n"
        "Ключи шифруются перед записью в базу и никогда не попадают в логи. "
        "Удали сообщение с ключом из чата после отправки — Telegram хранит "
        "историю на своих серверах.\n\n"
        "Пришли API Key:"
    )
    if creds is not None:
        text = f"Текущий ключ: {creds.api_key_masked}\n\n" + text

    await _reply(event, text, back_to(SetCB.API, with_menu=True))


@router.callback_query(F.data.startswith(SetCB.API_MODE))
async def ask_api_key(
    callback: CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    mode = ExchangeKeyMode(str(callback.data).removeprefix(SetCB.API_MODE))
    await _show_api_key_prompt(callback, state, user, session, mode)


@router.callback_query(SettingsStates.api_secret, F.data.startswith(SetCB.API_MODE_BACK))
async def back_to_api_key(
    callback: CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    mode = ExchangeKeyMode(str(callback.data).removeprefix(SetCB.API_MODE_BACK))
    await _show_api_key_prompt(callback, state, user, session, mode)


@router.message(SettingsStates.api_key)
async def receive_api_key(message: Message, state: FSMContext) -> None:
    key = (message.text or "").strip()
    if len(key) < 16:
        await message.answer("Ключ выглядит слишком коротким. Проверь и пришли ещё раз.")
        return

    data = await state.get_data()
    mode = ExchangeKeyMode(data["mode"])
    await state.update_data(api_key=key)
    await state.set_state(SettingsStates.api_secret)
    await message.answer(
        "Теперь пришли API Secret:",
        reply_markup=back_to(f"{SetCB.API_MODE_BACK}{mode.value}", with_menu=True),
    )


def _permission_verdict(creds: ExchangeCredentials) -> str:
    return "может торговать фьючерсами" if not creds.is_read_only else "только чтение"


def _last_known_permission_line(creds: ExchangeCredentials) -> str:
    """Раздел 8 ТЗ: при сбое запроса нельзя молча показать прежний
    результат как новый — пользователь решит, что проверка только что
    прошла успешно. Показываем явно, что это старые данные, и когда они
    получены (или что их вообще ещё нет)."""
    if creds.permissions_checked_at is None:
        return "Прежних данных о правах ключа нет."
    checked_at = creds.permissions_checked_at.strftime("%Y-%m-%d %H:%M UTC")
    return f"Последняя известная проверка: {checked_at}, {_permission_verdict(creds)}."


@router.message(SettingsStates.api_secret)
async def receive_api_secret(
    message: Message,
    state: FSMContext,
    user: User,
    session: AsyncSession,
    settings: Settings,
    cipher: SecretCipher,
) -> None:
    secret = (message.text or "").strip()
    if len(secret) < 16:
        await message.answer("Secret выглядит слишком коротким. Пришли ещё раз.")
        return

    data = await state.get_data()
    await state.clear()
    api_key = data["api_key"]
    mode = ExchangeKeyMode(data["mode"])

    creds = await _get_credentials(session, user.id, mode)
    if creds is None:
        creds = ExchangeCredentials(user_id=user.id, exchange="bingx", mode=mode)
        session.add(creds)

    creds.api_key_encrypted = cipher.encrypt(api_key)
    creds.api_secret_encrypted = cipher.encrypt(secret)
    creds.api_key_masked = mask_secret(api_key)
    creds.is_active = True

    await session.flush()

    # Раздел 8 ТЗ: права проверяются сразу, чтобы пользователь не думал,
    # что подключённый ключ уже готов торговать, если это не так.
    factory = ExchangeFactory(settings, cipher)
    try:
        client = await factory.for_user(session, user.id, mode=mode)
    except ExchangeAuthError:
        client = None  # свежерасшифрованный ключ не должен падать здесь,
        # но если всё же упал — не мешаем показать хотя бы сохранение ключа

    if client is None:
        rights_line = "Не удалось проверить права ключа — уточню перед следующим входом."
    else:
        try:
            outcome = await refresh_permissions(
                session, creds, client,
                ttl_hours=settings.exec_permissions_ttl_hours, force=True,
            )
        finally:
            await client.close()
        rights_line = (
            f"Не удалось опросить биржу: {outcome.error}. {_last_known_permission_line(creds)}"
            if outcome.error is not None
            else f"Ключ {_permission_verdict(creds)}."
        )

    await message.answer(
        f"✅ Ключи сохранены ({mode.label}): {creds.api_key_masked}\n"
        f"{rights_line}\n\n"
        f"Удали два предыдущих сообщения с ключами из этого чата.",
        reply_markup=back_to(SetCB.API, with_menu=True),
    )


@router.callback_query(F.data.startswith(SetCB.API_MODE_DELETE))
async def delete_api_key(
    callback: CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    mode = ExchangeKeyMode(str(callback.data).removeprefix(SetCB.API_MODE_DELETE))
    await state.clear()
    creds = await _get_credentials(session, user.id, mode)
    if creds is not None:
        await session.delete(creds)
        await session.flush()
    await _show_api_keys_menu(callback, state, user, session)


@router.callback_query(F.data.startswith(SetCB.API_MODE_CHECK))
async def check_api_permissions(
    callback: CallbackQuery,
    state: FSMContext,
    user: User,
    session: AsyncSession,
    settings: Settings,
    cipher: SecretCipher,
) -> None:
    """Раздел 8 ТЗ: принудительная проверка (force=True) — по нажатию
    пользователь ждёт результат сейчас, а не "когда-нибудь по TTL"."""
    mode = ExchangeKeyMode(str(callback.data).removeprefix(SetCB.API_MODE_CHECK))
    creds = await _get_credentials(session, user.id, mode)
    if creds is None:
        await callback.answer("Ключ не подключён.", show_alert=True)
        return

    factory = ExchangeFactory(settings, cipher)
    try:
        client = await factory.for_user(session, user.id, mode=mode)
    except ExchangeAuthError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    try:
        outcome = await refresh_permissions(
            session, creds, client, ttl_hours=settings.exec_permissions_ttl_hours, force=True
        )
    finally:
        await client.close()

    if outcome.error is not None:
        # Сбой — даже если сохранённая отметка формально ещё в пределах TTL,
        # пользователь только что явно попросил проверить СЕЙЧАС: показать
        # старое значение без оговорки выглядело бы так, будто проверка
        # прошла успешно (см. app/services/permissions.py).
        await callback.answer(
            f"Не удалось опросить биржу: {outcome.error}. {_last_known_permission_line(creds)}",
            show_alert=True,
        )
        return

    await callback.answer(f"{mode.label}: {_permission_verdict(creds)}.", show_alert=True)
    await _show_api_keys_menu(callback, state, user, session)


# ---------------------------------------------------------------------------
# Уведомления (этап 12)
# ---------------------------------------------------------------------------

# Порядок пунктов в требовании 6, дословно: сетапы READY, сетапы FORMING,
# приближение к TP/SL, дневная сводка, дневной лимит. "Графики к сигналам"
# добавлен позже сразу за FORMING — он относится к тем же уведомлениям.
# "Сводка исполнения" (этап 15.4, раздел 12а) добавлена в конец — отдельный
# переключатель, включён по умолчанию.
NOTIFICATION_ORDER = [
    "setup_ready",
    "setup_forming",
    "setup_charts",
    "tp_sl_approaching",
    "daily_report",
    "daily_limit_reached",
    "execution_digest",
]


def notifications_keyboard(notifications: dict) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key in NOTIFICATION_ORDER:
        enabled = notifications.get(key, DEFAULT_NOTIFICATIONS.get(key, True))
        mark = "✅ " if enabled else "⬜ "
        builder.button(
            text=f"{mark}{NOTIFICATION_LABELS[key]}", callback_data=f"{SetCB.NOTIFY}{key}"
        )
    builder.adjust(1)
    builder.row(*nav_row(MenuCallback.SETTINGS))
    return builder.as_markup()


async def _show_notifications(
    event: Message | CallbackQuery, user: User, session: AsyncSession
) -> None:
    settings_row = await UserRepository(session).get_settings(user.id)
    notifications = settings_row.notifications if settings_row is not None else {}
    await _reply(
        event,
        "<b>Уведомления</b>\n\n"
        "Тихие часы пока не поддерживаются — уведомления приходят "
        "круглосуточно.",
        notifications_keyboard(notifications),
    )


@router.callback_query(F.data == SetCB.NOTIFICATIONS)
async def show_notifications(callback: CallbackQuery, user: User, session: AsyncSession) -> None:
    await _show_notifications(callback, user, session)


@router.callback_query(F.data.startswith(SetCB.NOTIFY))
async def toggle_notification(
    callback: CallbackQuery, user: User, session: AsyncSession
) -> None:
    key = str(callback.data).removeprefix(SetCB.NOTIFY)
    if key not in NOTIFICATION_ORDER:
        await callback.answer()
        return

    settings_row = await UserRepository(session).get_settings(user.id)
    if settings_row is None:
        await callback.answer()
        return

    current = settings_row.notifications.get(key, DEFAULT_NOTIFICATIONS.get(key, True))
    # JSONB-словарь нужно переприсвоить целиком: мутация вложенного dict на
    # месте не помечает атрибут "изменённым" для SQLAlchemy при UPDATE.
    settings_row.notifications = {**settings_row.notifications, key: not current}
    await session.flush()

    await _show_notifications(callback, user, session)
