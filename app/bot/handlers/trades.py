"""Торговый журнал: добавление, просмотр и закрытие сделок.

Хендлеры не содержат расчётов и не обращаются к базе напрямую: их
задача — собрать ввод, отдать его сервису и отрисовать результат.
"""

from __future__ import annotations

from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.formatting import fmt_amount, fmt_num, fmt_qty
from app.bot.keyboards.main import (
    MenuCallback,
    back_to,
    back_to_main,
    main_menu,
    nav_row,
    with_menu_row,
    with_nav,
)
from app.bot.keyboards.trade import (
    TradeCB,
    mistakes_keyboard,
    open_trades_keyboard,
    quantity_mode_keyboard,
    side_keyboard,
    skip_keyboard,
    strategies_keyboard,
    symbols_keyboard,
    timeframe_keyboard,
    trade_card,
    trade_line,
)
from app.bot.states.trade import AddTradeStates, CloseTradeStates
from app.core.config import Settings
from app.core.logging import get_logger
from app.database.models.user import User
from app.database.repositories.strategy import (
    MistakeTypeRepository,
    StrategyRepository,
)
from app.database.repositories.trade import TradeRepository
from app.database.repositories.user import UserRepository
from app.trading.calculations import (
    CalculationError,
    calculate_position_size,
    calculate_risk_reward,
    to_decimal,
)
from app.trading.enums import TradeSide
from app.trading.journal import JournalError, TradeJournal
from app.trading.risk import PlanValidator

router = Router(name="trades")
logger = get_logger(__name__)


async def _parse_decimal(message: Message, field: str) -> Decimal | None:
    """Разбирает числовой ввод, сообщая пользователю о проблеме понятно."""
    try:
        value = to_decimal(message.text or "", field)
    except CalculationError:
        await message.answer(f"Не понял {field}. Введи число, например 100.5")
        return None
    if value <= 0:
        await message.answer(f"{field.capitalize()} должно быть больше нуля.")
        return None
    return value


# ---------------------------------------------------------------------------
# Добавление сделки
# ---------------------------------------------------------------------------


async def _send(event: Message | CallbackQuery, text: str, keyboard) -> None:  # type: ignore[no-untyped-def]
    """Отправляет шаг формы: edit для колбэка, новое сообщение для текста."""
    if isinstance(event, CallbackQuery):
        if isinstance(event.message, Message):
            await event.message.edit_text(text, reply_markup=keyboard)
        await event.answer()
    else:
        await event.answer(text, reply_markup=keyboard)


async def _show_symbol_prompt(
    event: Message | CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    """Шаг 1 — открывается из меню, «Назад» на нём нет, только «В меню»."""
    plan = await UserRepository(session).get_trading_plan(user.id)
    symbols = plan.allowed_symbols if plan else []
    await state.set_state(AddTradeStates.symbol)
    await _send(
        event,
        "<b>Новая сделка</b>\n\nВыбери инструмент или введи символ вручную:",
        with_menu_row(symbols_keyboard(symbols)),
    )


async def _show_side_prompt(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddTradeStates.side)
    data = await state.get_data()
    await _send(
        event,
        f"<b>{data['symbol']}</b>\n\nНаправление:",
        with_nav(side_keyboard(), parent_data=TradeCB.BACK),
    )


async def _show_entry_price_prompt(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddTradeStates.entry_price)
    await _send(event, "Цена входа:", back_to(TradeCB.BACK, with_menu=True))


async def _show_stop_loss_prompt(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddTradeStates.stop_loss)
    data = await state.get_data()
    side = TradeSide(data["side"])
    hint = "ниже" if side is TradeSide.LONG else "выше"
    await _send(
        event,
        f"Стоп-лосс (должен быть {hint} цены входа):\n\n"
        f"<i>Методология не допускает сделок без стопа, но записать такую "
        f"сделку в журнал можно — она будет отмечена как нарушение.</i>",
        with_nav(skip_keyboard(), parent_data=TradeCB.BACK, with_menu=True),
    )


async def _show_take_profit_prompt(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddTradeStates.take_profit)
    data = await state.get_data()
    text = "Take-profit:"
    if not data.get("stop_loss"):
        text = "⚠️ Сделка без стоп-лосса будет помечена как нарушение плана.\n\nTake-profit:"
    await _send(
        event, text, with_nav(skip_keyboard(), parent_data=TradeCB.BACK, with_menu=True)
    )


async def _ask_quantity_mode(
    event: Message | CallbackQuery, state: FSMContext
) -> None:
    await state.set_state(AddTradeStates.quantity_mode)
    data = await state.get_data()
    text = "Объём позиции:"
    if not data.get("stop_loss"):
        text = "Объём позиции (без стопа расчёт от риска недоступен):"
    await _send(
        event, text, with_nav(quantity_mode_keyboard(), parent_data=TradeCB.BACK, with_menu=True)
    )


async def _show_quantity_prompt(
    event: Message | CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    await state.set_state(AddTradeStates.quantity)
    data = await state.get_data()
    keyboard = back_to(TradeCB.BACK, with_menu=True)
    if data.get("quantity_mode") == "auto":
        plan = await UserRepository(session).get_trading_plan(user.id)
        risk = plan.risk_per_trade_percent if plan else Decimal("2")
        await _send(
            event,
            f"Введи баланс депозита в USDT.\n\n"
            f"Объём рассчитаю от риска {fmt_num(risk)}% по твоему плану.",
            keyboard,
        )
    else:
        await _send(event, "Объём в базовом активе (например 0.1):", keyboard)


async def _show_leverage_prompt(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddTradeStates.leverage)
    await _send(
        event,
        "Плечо (число, например 10):",
        with_nav(skip_keyboard(), parent_data=TradeCB.BACK, with_menu=True),
    )


async def _ask_strategy(
    event: Message | CallbackQuery,
    state: FSMContext,
    user: User,
    session: AsyncSession,
) -> None:
    await state.set_state(AddTradeStates.strategy)
    strategies = await StrategyRepository(session).list_active(user.id)
    keyboard = with_nav(strategies_keyboard(strategies), parent_data=TradeCB.BACK, with_menu=True)
    await _send(event, "Стратегия:", keyboard)


async def _show_timeframe_prompt(
    event: Message | CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    await state.set_state(AddTradeStates.timeframe)
    plan = await UserRepository(session).get_trading_plan(user.id)
    timeframes = plan.allowed_timeframes if plan else ["1h", "4h"]
    keyboard = with_nav(
        timeframe_keyboard(timeframes), parent_data=TradeCB.BACK, with_menu=True
    )
    await _send(event, "Таймфрейм:", keyboard)


async def _show_entry_reason_prompt(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddTradeStates.entry_reason)
    await _send(
        event,
        "Причина входа — что именно ты увидел на графике:",
        with_nav(skip_keyboard(), parent_data=TradeCB.BACK, with_menu=True),
    )


@router.callback_query(F.data == MenuCallback.ADD_TRADE)
@router.message(Command("trade"))
async def start_add_trade(
    event: Message | CallbackQuery,
    state: FSMContext,
    user: User,
    session: AsyncSession,
) -> None:
    await state.clear()
    await _show_symbol_prompt(event, state, user, session)


@router.callback_query(AddTradeStates.symbol, F.data.startswith(TradeCB.SYMBOL))
async def pick_symbol(callback: CallbackQuery, state: FSMContext) -> None:
    symbol = str(callback.data).removeprefix(TradeCB.SYMBOL)
    await state.update_data(symbol=symbol)
    await _show_side_prompt(callback, state)


@router.message(AddTradeStates.symbol)
async def type_symbol(message: Message, state: FSMContext) -> None:
    symbol = (message.text or "").strip().upper()
    if len(symbol) < 3:
        await message.answer("Введи символ, например BTC-USDT")
        return
    await state.update_data(symbol=symbol)
    await _show_side_prompt(message, state)


@router.callback_query(AddTradeStates.side, F.data == TradeCB.BACK)
async def side_back(
    callback: CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    await _show_symbol_prompt(callback, state, user, session)


@router.callback_query(AddTradeStates.side, F.data.startswith(TradeCB.SIDE))
async def pick_side(callback: CallbackQuery, state: FSMContext) -> None:
    side = str(callback.data).removeprefix(TradeCB.SIDE)
    await state.update_data(side=side)
    await _show_entry_price_prompt(callback, state)


@router.callback_query(AddTradeStates.entry_price, F.data == TradeCB.BACK)
async def entry_price_back(callback: CallbackQuery, state: FSMContext) -> None:
    await _show_side_prompt(callback, state)


@router.message(AddTradeStates.entry_price)
async def set_entry_price(message: Message, state: FSMContext) -> None:
    value = await _parse_decimal(message, "цену входа")
    if value is None:
        return
    await state.update_data(entry_price=str(value))
    await _show_stop_loss_prompt(message, state)


@router.callback_query(AddTradeStates.stop_loss, F.data == TradeCB.BACK)
async def stop_loss_back(callback: CallbackQuery, state: FSMContext) -> None:
    await _show_entry_price_prompt(callback, state)


@router.callback_query(AddTradeStates.stop_loss, F.data == TradeCB.SKIP)
async def skip_stop_loss(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(stop_loss=None)
    await _show_take_profit_prompt(callback, state)


@router.message(AddTradeStates.stop_loss)
async def set_stop_loss(message: Message, state: FSMContext) -> None:
    value = await _parse_decimal(message, "стоп-лосс")
    if value is None:
        return

    data = await state.get_data()
    side = TradeSide(data["side"])
    entry = Decimal(data["entry_price"])

    # Проверяем сторону стопа сразу: ловить ошибку на этапе подтверждения
    # значит заставить пользователя проходить форму заново.
    from app.trading.calculations import stop_distance

    try:
        stop_distance(entry_price=entry, stop_loss=value, side=side)
    except CalculationError as exc:
        await message.answer(f"⚠️ {exc}")
        return

    await state.update_data(stop_loss=str(value))
    await _show_take_profit_prompt(message, state)


@router.callback_query(AddTradeStates.take_profit, F.data == TradeCB.BACK)
async def take_profit_back(callback: CallbackQuery, state: FSMContext) -> None:
    await _show_stop_loss_prompt(callback, state)


@router.callback_query(AddTradeStates.take_profit, F.data == TradeCB.SKIP)
async def skip_take_profit(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(take_profit=None)
    await _ask_quantity_mode(callback, state)


@router.message(AddTradeStates.take_profit)
async def set_take_profit(message: Message, state: FSMContext) -> None:
    value = await _parse_decimal(message, "take-profit")
    if value is None:
        return

    data = await state.get_data()
    side = TradeSide(data["side"])
    entry = Decimal(data["entry_price"])

    if data.get("stop_loss"):
        try:
            rr = calculate_risk_reward(
                entry_price=entry,
                stop_loss=Decimal(data["stop_loss"]),
                take_profit=value,
                side=side,
            )
        except CalculationError as exc:
            await message.answer(f"⚠️ {exc}")
            return
        await message.answer(f"Плановый RR: 1:{fmt_num(rr)}")

    await state.update_data(take_profit=str(value))
    await _ask_quantity_mode(message, state)


@router.callback_query(AddTradeStates.quantity_mode, F.data == TradeCB.BACK)
async def quantity_mode_back(callback: CallbackQuery, state: FSMContext) -> None:
    await _show_take_profit_prompt(callback, state)


@router.callback_query(AddTradeStates.quantity_mode, F.data.startswith(TradeCB.QTY_MODE))
async def pick_quantity_mode(
    callback: CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    mode = str(callback.data).removeprefix(TradeCB.QTY_MODE)
    data = await state.get_data()

    if mode == "auto":
        if not data.get("stop_loss"):
            await callback.answer(
                "Без стоп-лосса объём от риска не рассчитать", show_alert=True
            )
            return
        await state.update_data(quantity_mode="auto")
    else:
        await state.update_data(quantity_mode="manual")

    await _show_quantity_prompt(callback, state, user, session)


@router.callback_query(AddTradeStates.quantity, F.data == TradeCB.BACK)
async def quantity_back(callback: CallbackQuery, state: FSMContext) -> None:
    await _ask_quantity_mode(callback, state)


@router.message(AddTradeStates.quantity)
async def set_quantity(
    message: Message, state: FSMContext, user: User, session: AsyncSession
) -> None:
    data = await state.get_data()

    if data.get("quantity_mode") == "auto":
        balance = await _parse_decimal(message, "баланс")
        if balance is None:
            return

        plan = await UserRepository(session).get_trading_plan(user.id)
        risk_percent = plan.risk_per_trade_percent if plan else Decimal("2")

        try:
            sizing = calculate_position_size(
                account_balance=balance,
                risk_percent=risk_percent,
                entry_price=Decimal(data["entry_price"]),
                stop_loss=Decimal(data["stop_loss"]),
                side=TradeSide(data["side"]),
            )
        except CalculationError as exc:
            await message.answer(f"⚠️ {exc}")
            return

        await state.update_data(
            quantity=str(sizing.quantity), account_balance=str(balance)
        )
        await message.answer(
            f"<b>Расчёт позиции</b>\n\n"
            f"Сумма риска: {fmt_amount(sizing.risk_amount)} USDT\n"
            f"Дистанция до стопа: {fmt_num(sizing.stop_distance_percent)}%\n"
            f"Объём: {fmt_qty(sizing.quantity)}\n"
            f"Размер позиции: {fmt_amount(sizing.position_value)} USDT"
        )
    else:
        quantity = await _parse_decimal(message, "объём")
        if quantity is None:
            return
        await state.update_data(quantity=str(quantity))

    await _show_leverage_prompt(message, state)


@router.callback_query(AddTradeStates.leverage, F.data == TradeCB.BACK)
async def leverage_back(
    callback: CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    await _show_quantity_prompt(callback, state, user, session)


@router.callback_query(AddTradeStates.leverage, F.data == TradeCB.SKIP)
async def skip_leverage(
    callback: CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    await state.update_data(leverage=1)
    await _ask_strategy(callback, state, user, session)


@router.message(AddTradeStates.leverage)
async def set_leverage(
    message: Message, state: FSMContext, user: User, session: AsyncSession
) -> None:
    try:
        leverage = int((message.text or "").strip().rstrip("xX"))
    except ValueError:
        await message.answer("Введи целое число, например 10")
        return
    if leverage < 1:
        await message.answer("Плечо не может быть меньше 1")
        return

    await state.update_data(leverage=leverage)
    await _ask_strategy(message, state, user, session)


@router.callback_query(AddTradeStates.strategy, F.data == TradeCB.BACK)
async def strategy_back(callback: CallbackQuery, state: FSMContext) -> None:
    await _show_leverage_prompt(callback, state)


@router.callback_query(AddTradeStates.strategy, F.data.startswith(TradeCB.STRATEGY))
async def pick_strategy(
    callback: CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    strategy_id = int(str(callback.data).removeprefix(TradeCB.STRATEGY))
    await state.update_data(strategy_id=strategy_id or None)
    await _show_timeframe_prompt(callback, state, user, session)


@router.callback_query(AddTradeStates.timeframe, F.data == TradeCB.BACK)
async def timeframe_back(
    callback: CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    await _ask_strategy(callback, state, user, session)


@router.callback_query(AddTradeStates.timeframe, F.data != TradeCB.BACK)
async def pick_timeframe(callback: CallbackQuery, state: FSMContext) -> None:
    data_str = str(callback.data)
    timeframe = (
        None if data_str == TradeCB.SKIP else data_str.removeprefix(TradeCB.TIMEFRAME)
    )
    await state.update_data(timeframe=timeframe)
    await _show_entry_reason_prompt(callback, state)


@router.callback_query(AddTradeStates.entry_reason, F.data == TradeCB.BACK)
async def entry_reason_back(
    callback: CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    await _show_timeframe_prompt(callback, state, user, session)


@router.callback_query(AddTradeStates.entry_reason, F.data == TradeCB.SKIP)
async def skip_entry_reason(
    callback: CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    await state.update_data(entry_reason=None)
    await _save_trade(callback, state, user, session)


@router.message(AddTradeStates.entry_reason)
async def set_entry_reason(
    message: Message, state: FSMContext, user: User, session: AsyncSession
) -> None:
    await state.update_data(entry_reason=(message.text or "").strip()[:1000])
    await _save_trade(message, state, user, session)


async def _save_trade(
    event: Message | CallbackQuery,
    state: FSMContext,
    user: User,
    session: AsyncSession,
) -> None:
    """Проверяет план, сохраняет сделку, показывает карточку.

    Нарушения плана показываются, но не блокируют сохранение: журнал
    обязан отражать реальную торговлю, включая сделки вне правил.
    """
    data = await state.get_data()
    await state.clear()

    repo = TradeRepository(session)
    journal = TradeJournal(repo)
    plan = await UserRepository(session).get_trading_plan(user.id)

    entry_price = Decimal(data["entry_price"])
    quantity = Decimal(data["quantity"])
    stop_loss = Decimal(data["stop_loss"]) if data.get("stop_loss") else None
    take_profit = Decimal(data["take_profit"]) if data.get("take_profit") else None
    balance = Decimal(data["account_balance"]) if data.get("account_balance") else None
    side = TradeSide(data["side"])

    warning = ""
    if plan is not None:
        check = await PlanValidator(repo).check(
            plan=plan,
            user_id=user.id,
            symbol=data["symbol"],
            side=side,
            entry_price=entry_price,
            quantity=quantity,
            stop_loss=stop_loss,
            take_profit=take_profit,
            leverage=int(data.get("leverage", 1)),
            timeframe=data.get("timeframe"),
            account_balance=balance,
        )
        if not check.ok:
            warning = "\n\n" + check.render()

    try:
        trade = await journal.open_trade(
            user_id=user.id,
            symbol=data["symbol"],
            side=side,
            entry_price=entry_price,
            quantity=quantity,
            stop_loss=stop_loss,
            take_profit=take_profit,
            leverage=int(data.get("leverage", 1)),
            strategy_id=data.get("strategy_id"),
            timeframe=data.get("timeframe"),
            entry_reason=data.get("entry_reason"),
            account_balance=balance,
        )
    except (JournalError, CalculationError) as exc:
        text = f"⚠️ Не удалось сохранить сделку: {exc}"
        if isinstance(event, CallbackQuery):
            if isinstance(event.message, Message):
                await event.message.edit_text(text, reply_markup=main_menu())
            await event.answer()
        else:
            await event.answer(text, reply_markup=main_menu())
        return

    await session.refresh(trade, ["strategy"])
    text = "✅ <b>Сделка записана</b>\n\n" + trade_card(trade) + warning

    if isinstance(event, CallbackQuery):
        if isinstance(event.message, Message):
            await event.message.edit_text(text, reply_markup=main_menu())
        await event.answer()
    else:
        await event.answer(text, reply_markup=main_menu())


# ---------------------------------------------------------------------------
# Просмотр
# ---------------------------------------------------------------------------


@router.callback_query(F.data == MenuCallback.OPEN_POSITIONS)
@router.message(Command("open"))
async def show_open_positions(
    event: Message | CallbackQuery, user: User, session: AsyncSession
) -> None:
    trades = await TradeRepository(session).list_open(user.id)

    if not trades:
        text = "Открытых позиций нет."
        keyboard = back_to_main()
    else:
        lines = ["<b>Открытые позиции</b>", ""]
        lines += [trade_line(t) for t in trades]
        lines += ["", "Нажми на позицию, чтобы закрыть её."]
        text = "\n".join(lines)
        keyboard = open_trades_keyboard(trades)

    if isinstance(event, CallbackQuery):
        if isinstance(event.message, Message):
            await event.message.edit_text(text, reply_markup=keyboard)
        await event.answer()
    else:
        await event.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == MenuCallback.TRADES)
@router.message(Command("trades"))
async def show_recent_trades(
    event: Message | CallbackQuery, user: User, session: AsyncSession
) -> None:
    trades = await TradeRepository(session).list_recent(user.id, limit=10)

    if not trades:
        text = "Сделок пока нет. Добавь первую через меню."
    else:
        lines = ["<b>Последние сделки</b>", ""]
        lines += [trade_line(t) for t in trades]
        text = "\n".join(lines)

    if isinstance(event, CallbackQuery):
        if isinstance(event.message, Message):
            await event.message.edit_text(text, reply_markup=back_to_main())
        await event.answer()
    else:
        await event.answer(text, reply_markup=back_to_main())


# ---------------------------------------------------------------------------
# Закрытие
# ---------------------------------------------------------------------------


async def _show_exit_price_prompt(
    event: Message | CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    data = await state.get_data()
    trade = await TradeRepository(session).get(int(data["trade_id"]), user.id)
    await state.set_state(CloseTradeStates.exit_price)
    text = "Сделка не найдена." if trade is None else trade_card(trade) + "\n\nЦена выхода:"
    await _reply_close(event, text, back_to(MenuCallback.OPEN_POSITIONS))


async def _show_fee_prompt(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CloseTradeStates.fee)
    await _reply_close(
        event, "Комиссия в USDT:", with_nav(skip_keyboard(), parent_data=TradeCB.CLOSE_BACK, with_menu=True)
    )


async def _show_exit_reason_prompt(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CloseTradeStates.exit_reason)
    await _reply_close(
        event,
        "Причина выхода:",
        with_nav(skip_keyboard(), parent_data=TradeCB.CLOSE_BACK, with_menu=True),
    )


@router.callback_query(F.data.startswith(TradeCB.CLOSE))
async def start_close_trade(
    callback: CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    trade_id = int(str(callback.data).removeprefix(TradeCB.CLOSE))
    trade = await TradeRepository(session).get(trade_id, user.id)

    if trade is None or not trade.is_open:
        await callback.answer("Сделка недоступна или уже закрыта", show_alert=True)
        return

    await state.update_data(trade_id=trade_id)
    await _show_exit_price_prompt(callback, state, user, session)
    await callback.answer()


@router.message(CloseTradeStates.exit_price)
async def set_exit_price(message: Message, state: FSMContext) -> None:
    value = await _parse_decimal(message, "цену выхода")
    if value is None:
        return
    await state.update_data(exit_price=str(value))
    await _show_fee_prompt(message, state)


@router.callback_query(CloseTradeStates.fee, F.data == TradeCB.CLOSE_BACK)
async def fee_back(
    callback: CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    await _show_exit_price_prompt(callback, state, user, session)


@router.callback_query(CloseTradeStates.fee, F.data == TradeCB.SKIP)
async def skip_fee(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(fee="0")
    await _show_exit_reason_prompt(callback, state)


@router.message(CloseTradeStates.fee)
async def set_fee(message: Message, state: FSMContext) -> None:
    try:
        fee = to_decimal(message.text or "", "комиссию")
    except CalculationError:
        await message.answer("Введи число, например 1.25")
        return
    await state.update_data(fee=str(fee))
    await _show_exit_reason_prompt(message, state)


@router.callback_query(CloseTradeStates.exit_reason, F.data == TradeCB.CLOSE_BACK)
async def exit_reason_back(callback: CallbackQuery, state: FSMContext) -> None:
    await _show_fee_prompt(callback, state)


@router.callback_query(CloseTradeStates.exit_reason, F.data == TradeCB.SKIP)
async def skip_exit_reason(
    callback: CallbackQuery,
    state: FSMContext,
    user: User,
    session: AsyncSession,
    settings: Settings,
) -> None:
    await _finish_close(callback, state, user, session, settings, reason=None)


@router.message(CloseTradeStates.exit_reason)
async def set_exit_reason(
    message: Message,
    state: FSMContext,
    user: User,
    session: AsyncSession,
    settings: Settings,
) -> None:
    await _finish_close(
        message, state, user, session, settings,
        reason=(message.text or "").strip()[:1000],
    )


async def _finish_close(
    event: Message | CallbackQuery,
    state: FSMContext,
    user: User,
    session: AsyncSession,
    settings: Settings,
    reason: str | None,
) -> None:
    """Закрывает сделку и предлагает отметить ошибки.

    Результат сохраняется до вопроса про ошибки: если пользователь бросит
    диалог на этом шаге, сделка всё равно останется закрытой с верным PnL.
    """
    data = await state.get_data()

    repo = TradeRepository(session)
    trade = await repo.get(int(data["trade_id"]), user.id)
    if trade is None:
        await state.clear()
        await _reply_close(event, "Сделка не найдена.", main_menu())
        return

    try:
        await TradeJournal(repo).close_trade(
            trade,
            exit_price=Decimal(data["exit_price"]),
            fee=Decimal(data.get("fee", "0")),
            exit_reason=reason,
        )
    except (JournalError, CalculationError) as exc:
        await state.clear()
        await _reply_close(event, f"⚠️ {exc}", main_menu())
        return

    card = "✅ <b>Сделка закрыта</b>\n\n" + trade_card(trade)

    mistake_repo = MistakeTypeRepository(session)
    mistakes = await mistake_repo.list_available(user.id)

    if not mistakes:
        # Справочник наполняется при старте приложения, но если это не
        # отработало, пользователь молча не увидел бы вопрос про ошибки:
        # шаг просто пропустился бы, а раздел «Анализ ошибок» остался бы
        # пустым навсегда. Восстанавливаем ровно там, где список нужен.
        await _seed_system_mistakes(session, settings)
        mistakes = await mistake_repo.list_available(user.id)

    if not mistakes:
        await state.clear()
        await _reply_close(event, card, main_menu())
        return

    await state.set_state(CloseTradeStates.mistakes)
    await state.update_data(trade_id=trade.id, selected=[])

    await _reply_close(
        event,
        card + "\n\n<b>Были ошибки в этой сделке?</b>\n"
        "<i>Отметь все подходящие и нажми «Готово». "
        "Без разметки анализ повторяющихся ошибок работать не будет.</i>",
        mistakes_keyboard(mistakes, set()),
    )


async def _seed_system_mistakes(session: AsyncSession, settings: Settings) -> None:
    """Досоздаёт системный справочник ошибок. Идемпотентно."""
    from app.services.user_service import UserService

    service = UserService(
        UserRepository(session),
        StrategyRepository(session),
        MistakeTypeRepository(session),
        settings,
    )
    created = await service.ensure_system_mistakes()
    if created:
        # Имя ключа не должно совпадать с полями LogRecord: logging
        # роняет вызов с KeyError, если в extra попадает, например,
        # "created" или "message".
        logger.warning(
            "Справочник ошибок был пуст и восстановлен",
            extra={"restored_count": created},
        )


async def _reply_close(event: Message | CallbackQuery, text: str, keyboard) -> None:  # type: ignore[no-untyped-def]
    if isinstance(event, CallbackQuery):
        if isinstance(event.message, Message):
            await event.message.edit_text(text, reply_markup=keyboard)
        await event.answer()
    else:
        await event.answer(text, reply_markup=keyboard)


@router.callback_query(CloseTradeStates.mistakes, F.data.startswith(TradeCB.MISTAKE))
async def toggle_mistake(
    callback: CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    """Переключает отметку ошибки, оставляя пользователя в том же экране."""
    mistake_id = int(str(callback.data).removeprefix(TradeCB.MISTAKE))
    data = await state.get_data()
    selected = set(data.get("selected", []))

    if mistake_id in selected:
        selected.remove(mistake_id)
    else:
        selected.add(mistake_id)
    await state.update_data(selected=sorted(selected))

    mistakes = await MistakeTypeRepository(session).list_available(user.id)
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(
            reply_markup=mistakes_keyboard(mistakes, selected)
        )
    await callback.answer()


@router.callback_query(CloseTradeStates.mistakes, F.data == TradeCB.MISTAKES_DONE)
async def finish_mistakes(
    callback: CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    data = await state.get_data()
    await state.clear()

    repo = TradeRepository(session)
    trade = await repo.get(int(data["trade_id"]), user.id)
    selected = list(data.get("selected", []))

    if trade is not None and selected:
        await TradeJournal(repo).annotate(trade, mistake_type_ids=selected)
        await session.refresh(trade, ["mistakes"])

    text = (
        "✅ <b>Сделка закрыта</b>\n\n" + trade_card(trade)
        if trade is not None
        else "Сделка не найдена."
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=main_menu())
    await callback.answer()
