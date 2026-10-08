"""Чат-мастер «Открыть на бирже» (05.10.2026, docs/open-trade-plan.md §9).

«➕ Добавить сделку» начинается с выбора: открыть на бирже или только записать
в журнал (прежний мастер). Здесь — путь «Открыть на бирже»: монета (поиск по
контрактам BingX), сторона, тип входа, цена и срок лимита, стоп, тейк, риск %,
плечо → карточка подтверждения → «Открыть». Хендлеры тонкие: ввод → ядро
(app/execution/opening) → текст. Переписка мастера удаляется после открытия
(WizardTrailMiddleware); карточка правится в итог и остаётся.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.exchange import _market_cache
from app.bot.handlers.positions import position_keyboard
from app.bot.handlers.trades import _show_symbol_prompt
from app.bot.keyboards.main import MenuCallback, with_nav
from app.bot.prompts import ask_number
from app.bot.states.trade import OpenTradeStates
from app.bot.wizard_trail import remember
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.numfmt import fmt_pct
from app.core.security import SecretCipher
from app.database.models.user import User
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import SymbolInfo
from app.execution.callback_audit import record_callback
from app.execution.opening.calc import OpeningInputs
from app.execution.opening.render import expiry_label
from app.execution.opening.service import (
    LIMIT_EXPIRY_CHOICES,
    ConfirmOutcome,
    OpeningService,
)
from app.market.data import MarketDataService
from app.services.exchange_factory import ExchangeFactory
from app.trading.calculations import CalculationError, to_decimal
from app.trading.enums import (
    EntryType,
    ExecutionCallbackAction,
    OpeningSource,
    OpeningStatus,
    TradeSide,
)

router = Router(name="open_trade")
logger = get_logger(__name__)

AUDIT_FAILED_TEXT = (
    "Не удалось записать нажатие — открытие не выполнено, на биржу ничего не "
    "отправлено. Попробуй ещё раз."
)
SEARCH_LIMIT = 6


class OpenCB:
    MODE_EXCHANGE = "ot:mode:x"
    MODE_JOURNAL = "ot:mode:j"
    SYMBOL = "ot:sym:"        # + SYMBOL
    SIDE = "ot:side:"         # + LONG|SHORT
    TYPE = "ot:type:"         # + MARKET|LIMIT
    EXPIRY = "ot:exp:"        # + минуты
    NO_TAKE = "ot:notake"
    RISK_PLAN = "ot:risk:plan"
    LEVERAGE = "ot:lev:"      # + плечо
    BACK = "ot:back:"         # + шаг
    YES = "ot:y:"             # + opening_id
    YES_WARN = "ot:w:"        # + opening_id
    NO = "ot:n:"              # + opening_id
    CANCEL_LIMIT = "ot:c:"    # + opening_id
    RECALC = "ot:recalc"
    EDIT = "ot:edit"


def mode_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text="🟢 Открыть на бирже", callback_data=OpenCB.MODE_EXCHANGE
    ))
    builder.row(InlineKeyboardButton(
        text="📝 Только записать в журнал", callback_data=OpenCB.MODE_JOURNAL
    ))
    builder.row(InlineKeyboardButton(text="◀️ В меню", callback_data=MenuCallback.MAIN))
    return builder.as_markup()


def _rows(*rows: list[tuple[str, str]], back: str | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for row in rows:
        builder.row(*[InlineKeyboardButton(text=t, callback_data=d) for t, d in row])
    markup = builder.as_markup()
    if back is not None:
        return with_nav(markup, parent_data=f"{OpenCB.BACK}{back}", with_menu=True)
    return markup


async def _send(event: Message | CallbackQuery, state: FSMContext, text: str,
                keyboard: InlineKeyboardMarkup | None) -> Message | None:
    """Шаг мастера: edit для кнопки, новое сообщение для текста."""
    if isinstance(event, CallbackQuery):
        await event.answer()
        if isinstance(event.message, Message):
            await event.message.edit_text(text, reply_markup=keyboard)
            await remember(state, event.message.message_id)
            return event.message
        return None
    sent = await event.answer(text, reply_markup=keyboard)
    await remember(state, sent.message_id)
    return sent


async def _say(message: Message, state: FSMContext, text: str) -> None:
    sent = await message.answer(text)
    await remember(state, sent.message_id)


def _number(text: str | None) -> Decimal | None:
    try:
        value = to_decimal((text or "").replace(",", "."), "число")
    except CalculationError:
        return None
    return value if value > 0 else None


# --- начало: выбор пути -------------------------------------------------------------


async def show_mode(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(OpenTradeStates.mode)
    await _send(
        event, state,
        "<b>Новая сделка</b>\n\nОткрыть позицию на бирже из бота или только записать "
        "сделку в журнал?",
        mode_keyboard(),
    )


@router.callback_query(F.data == MenuCallback.ADD_TRADE)
@router.message(Command("trade"))
async def start_add_trade(event: Message | CallbackQuery, state: FSMContext) -> None:
    await show_mode(event, state)


@router.callback_query(OpenTradeStates.mode, F.data == OpenCB.MODE_JOURNAL)
async def pick_journal(
    callback: CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    await _show_symbol_prompt(callback, state, user, session)


@router.callback_query(OpenTradeStates.mode, F.data == OpenCB.MODE_EXCHANGE)
async def pick_exchange(
    callback: CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    await _show_symbol(callback, state, user, session)


# --- монета ---------------------------------------------------------------------------


async def _show_symbol(
    event: Message | CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    plan = await UserRepository(session).get_trading_plan(user.id)
    allowed = plan.allowed_symbols if plan else []
    quick = [(s.replace("-USDT", ""), f"{OpenCB.SYMBOL}{s}") for s in allowed]
    rows = [quick[i:i + 4] for i in range(0, len(quick), 4)]
    await state.set_state(OpenTradeStates.symbol)
    await _send(
        event, state,
        "<b>🟢 Открыть на бирже</b>\n\nМонета: выбери или напиши тикер (XRP, DOGE, PEPE…).",
        _rows(*rows, back="mode"),
    )


async def search_contracts(settings: Settings, query: str) -> list[SymbolInfo]:
    """Контракты BingX по тикеру: точное совпадение — одно; иначе по началу и
    вхождению базового актива (публичный список, общий кэш экранов)."""
    q = query.strip().upper().replace("/", "-").removesuffix("-USDT").removesuffix("USDT")
    q = q.strip("-")
    if not q:
        return []
    client = ExchangeFactory(settings, None).public_client()  # type: ignore[arg-type]
    try:
        symbols = await MarketDataService(client, _market_cache).get_symbols()
    finally:
        await client.close()
    usdt = [s for s in symbols if s.symbol.endswith("-USDT")]
    exact = [s for s in usdt if s.symbol == f"{q}-USDT"]
    if exact:
        return exact
    starts = sorted((s for s in usdt if s.symbol.startswith(q)), key=lambda s: s.symbol)
    inside = sorted(
        (s for s in usdt if q in s.symbol.removesuffix("-USDT") and s not in starts),
        key=lambda s: s.symbol,
    )
    return [*starts, *inside][:SEARCH_LIMIT]


@router.message(OpenTradeStates.symbol)
async def type_symbol(message: Message, state: FSMContext, settings: Settings) -> None:
    try:
        found = await search_contracts(settings, message.text or "")
    except Exception:
        logger.warning("Поиск контракта не удался", exc_info=True)
        await _say(message, state, "Список контрактов BingX сейчас недоступен — попробуй ещё раз.")
        return
    if not found:
        await _say(message, state, f"Не нашёл контракт «{(message.text or '').strip()}» на BingX — "
                                   "проверь тикер.")
        return
    if len(found) == 1:
        await state.update_data(symbol=found[0].symbol)
        await _show_side(message, state)
        return
    buttons = [(s.symbol.replace("-USDT", ""), f"{OpenCB.SYMBOL}{s.symbol}") for s in found]
    await _send(message, state, "Нашёл несколько — выбери:",
                _rows(*[buttons[i:i + 3] for i in range(0, len(buttons), 3)], back="mode"))


@router.callback_query(OpenTradeStates.symbol, F.data.startswith(OpenCB.SYMBOL))
async def pick_symbol(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(symbol=str(callback.data).removeprefix(OpenCB.SYMBOL))
    await _show_side(callback, state)


# --- сторона, тип, лимит ----------------------------------------------------------------


async def _show_side(event: Message | CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(OpenTradeStates.side)
    await _send(event, state, f"<b>{data['symbol']}</b>\n\nНаправление:", _rows(
        [("🟢 LONG", f"{OpenCB.SIDE}LONG"), ("🔴 SHORT", f"{OpenCB.SIDE}SHORT")], back="symbol",
    ))


@router.callback_query(OpenTradeStates.side, F.data.startswith(OpenCB.SIDE))
async def pick_side(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(side=str(callback.data).removeprefix(OpenCB.SIDE))
    await _show_type(callback, state)


async def _show_type(event: Message | CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(OpenTradeStates.entry_type)
    await _send(event, state, f"<b>{data['symbol']} {data['side']}</b>\n\nВход:", _rows(
        [("По рынку", f"{OpenCB.TYPE}MARKET"), ("Лимитный", f"{OpenCB.TYPE}LIMIT")],
        back="side",
    ))


@router.callback_query(OpenTradeStates.entry_type, F.data.startswith(OpenCB.TYPE))
async def pick_type(callback: CallbackQuery, state: FSMContext) -> None:
    entry_type = str(callback.data).removeprefix(OpenCB.TYPE)
    await state.update_data(entry_type=entry_type)
    if entry_type == EntryType.LIMIT.value:
        await _show_limit_price(callback, state)
    else:
        await state.update_data(limit_price=None, expiry=None)
        await _show_stop(callback, state)


async def _show_limit_price(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(OpenTradeStates.limit_price)
    await _send(event, state, "Цена лимитного ордера:", _rows(back="type"))
    await ask_number(event, state, "Цена лимита числом")


@router.message(OpenTradeStates.limit_price)
async def set_limit_price(message: Message, state: FSMContext, settings: Settings) -> None:
    value = _number(message.text)
    if value is None:
        await _say(message, state, "Нужна цена числом больше нуля, например 1.4800")
        return
    await state.update_data(limit_price=str(value))
    await _show_expiry(message, state, settings)


async def _show_expiry(
    event: Message | CallbackQuery, state: FSMContext, settings: Settings
) -> None:
    default = settings.exec_open_limit_expiry_minutes
    buttons = [
        (expiry_label(m) + (" ✓" if m == default else ""), f"{OpenCB.EXPIRY}{m}")
        for m in LIMIT_EXPIRY_CHOICES
    ]
    await state.set_state(OpenTradeStates.expiry)
    await _send(event, state, "Срок лимита — бот отменит ордер, если он не исполнится:",
                _rows(buttons, back="limit"))


@router.callback_query(OpenTradeStates.expiry, F.data.startswith(OpenCB.EXPIRY))
async def pick_expiry(callback: CallbackQuery, state: FSMContext) -> None:
    minutes = int(str(callback.data).removeprefix(OpenCB.EXPIRY))
    if minutes not in LIMIT_EXPIRY_CHOICES:
        await callback.answer("Кнопка устарела.", show_alert=True)
        return
    await state.update_data(expiry=minutes)
    await _show_stop(callback, state)


# --- стоп, тейк, риск, плечо ---------------------------------------------------------------


async def _show_stop(event: Message | CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(OpenTradeStates.stop_loss)
    back = "expiry" if data.get("entry_type") == EntryType.LIMIT.value else "type"
    await _send(event, state, "Стоп-лосс (обязателен):", _rows(back=back))
    await ask_number(event, state, "Цена стопа числом")


@router.message(OpenTradeStates.stop_loss)
async def set_stop(message: Message, state: FSMContext) -> None:
    value = _number(message.text)
    if value is None:
        await _say(message, state, "Нужна цена стопа числом больше нуля.")
        return
    await state.update_data(stop=str(value))
    await _show_take(message, state)


async def _show_take(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(OpenTradeStates.take_profit)
    await _send(event, state, "Тейк-профит:", _rows([("Без тейка", OpenCB.NO_TAKE)], back="stop"))
    await ask_number(event, state, "Цена тейка числом")


@router.message(OpenTradeStates.take_profit)
async def set_take(message: Message, state: FSMContext, user: User, session: AsyncSession) -> None:
    value = _number(message.text)
    if value is None:
        await _say(message, state, "Нужна цена тейка числом — или нажми «Без тейка».")
        return
    await state.update_data(take=str(value))
    await _show_risk(message, state, user, session)


@router.callback_query(OpenTradeStates.take_profit, F.data == OpenCB.NO_TAKE)
async def skip_take(callback: CallbackQuery, state: FSMContext, user: User,
                    session: AsyncSession) -> None:
    await state.update_data(take=None)
    await _show_risk(callback, state, user, session)


async def _plan_risk(session: AsyncSession, user: User) -> Decimal | None:
    plan = await UserRepository(session).get_trading_plan(user.id)
    return plan.risk_per_trade_percent if plan else None


async def _show_risk(
    event: Message | CallbackQuery, state: FSMContext, user: User, session: AsyncSession
) -> None:
    plan_risk = await _plan_risk(session, user)
    await state.set_state(OpenTradeStates.risk)
    rows = [[(f"По плану {fmt_pct(plan_risk)}", OpenCB.RISK_PLAN)]] if plan_risk else []
    text = "Риск на сделку, % от equity (с комиссией):"
    if plan_risk:
        text += f"\nМаксимум по плану — {fmt_pct(plan_risk)}."
    await _send(event, state, text, _rows(*rows, back="take"))
    await ask_number(event, state, "Риск в процентах, например 1")


@router.callback_query(OpenTradeStates.risk, F.data == OpenCB.RISK_PLAN)
async def pick_plan_risk(callback: CallbackQuery, state: FSMContext, user: User,
                         session: AsyncSession, settings: Settings, cipher: SecretCipher,
                         redis: Any) -> None:
    risk = await _plan_risk(session, user)
    await state.update_data(risk=str(risk))
    await _show_leverage(callback, state, user, session, settings, cipher, redis)


@router.message(OpenTradeStates.risk)
async def set_risk(message: Message, state: FSMContext, user: User, session: AsyncSession,
                   settings: Settings, cipher: SecretCipher, redis: Any) -> None:
    value = _number((message.text or "").replace("%", ""))
    if value is None:
        await _say(message, state, "Нужен риск в процентах числом, например 1 или 0.5.")
        return
    plan_risk = await _plan_risk(session, user)
    if plan_risk is not None and value > plan_risk:
        await _say(message, state, f"Нельзя: максимум по плану — {fmt_pct(plan_risk)}.")
        return
    await state.update_data(risk=str(value))
    await _show_leverage(message, state, user, session, settings, cipher, redis)


def _service(session: AsyncSession, settings: Settings, cipher: SecretCipher, user: User,
             redis: Any) -> OpeningService:
    return OpeningService(session, settings, cipher, user, redis=redis,
                          market_cache=_market_cache)


def _inputs(data: dict[str, Any], leverage: int) -> OpeningInputs:
    return OpeningInputs(
        symbol=data["symbol"], side=TradeSide(data["side"]),
        entry_type=EntryType(data["entry_type"]), stop_loss=Decimal(data["stop"]),
        risk_percent=Decimal(data["risk"]), leverage=leverage,
        limit_price=Decimal(data["limit_price"]) if data.get("limit_price") else None,
        take_profit=Decimal(data["take"]) if data.get("take") else None,
        expiry_minutes=data.get("expiry"),
    )


async def _show_leverage(
    event: Message | CallbackQuery, state: FSMContext, user: User, session: AsyncSession,
    settings: Settings, cipher: SecretCipher, redis: Any,
) -> None:
    """Плечо: предложение ядра — максимум при этом стопе (запас ликвидации),
    не выше биржи и плана."""
    data = await state.get_data()
    preview = await _service(session, settings, cipher, user, redis).preview(_inputs(data, 1))
    if preview.calc is None:
        await state.clear()
        await _send(event, state, preview.text, None)
        return
    plan = await UserRepository(session).get_trading_plan(user.id)
    suggested = preview.calc.suggested_leverage
    buttons = [(f"{suggested}x (предложено)", f"{OpenCB.LEVERAGE}{suggested}")]
    for lev in (5, 10):
        if lev < suggested:
            buttons.append((f"{lev}x", f"{OpenCB.LEVERAGE}{lev}"))
    await state.set_state(OpenTradeStates.leverage)
    text = (
        f"Плечо. При этом стопе — не больше {preview.calc.max_leverage_for_stop}x "
        f"(ликвидация дальше стопа в {settings.exec_open_liq_buffer} раза)"
    )
    if plan is not None:
        text += f", по плану — не больше {plan.max_leverage}x"
    await _send(event, state, text + ".", _rows(buttons, back="risk"))
    await ask_number(event, state, "Плечо целым числом")


@router.callback_query(OpenTradeStates.leverage, F.data.startswith(OpenCB.LEVERAGE))
async def pick_leverage(callback: CallbackQuery, state: FSMContext, user: User,
                        session: AsyncSession, settings: Settings, cipher: SecretCipher,
                        redis: Any) -> None:
    await state.update_data(leverage=int(str(callback.data).removeprefix(OpenCB.LEVERAGE)))
    await _show_card(callback, state, user, session, settings, cipher, redis)


@router.message(OpenTradeStates.leverage)
async def set_leverage(message: Message, state: FSMContext, user: User, session: AsyncSession,
                       settings: Settings, cipher: SecretCipher, redis: Any) -> None:
    text = (message.text or "").strip().lower().rstrip("xх")
    if not text.isdigit() or int(text) < 1:
        await _say(message, state, "Плечо — целое число от 1, например 10.")
        return
    await state.update_data(leverage=int(text))
    await _show_card(message, state, user, session, settings, cipher, redis)


# --- карточка и «Открыть» ---------------------------------------------------------------------


def card_keyboard(
    opening_id: int | None, *, can_open: bool, warnings: bool
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if can_open and opening_id is not None:
        if warnings:
            builder.row(InlineKeyboardButton(
                text="⚠️ Открыть всё равно", callback_data=f"{OpenCB.YES_WARN}{opening_id}"
            ))
        else:
            builder.row(InlineKeyboardButton(
                text="✅ Открыть", callback_data=f"{OpenCB.YES}{opening_id}"
            ))
    row = [InlineKeyboardButton(text="✏️ Изменить", callback_data=OpenCB.EDIT)]
    if opening_id is not None:
        row.append(InlineKeyboardButton(text="✖️ Отмена", callback_data=f"{OpenCB.NO}{opening_id}"))
    builder.row(*row)
    return builder.as_markup()


async def _show_card(
    event: Message | CallbackQuery, state: FSMContext, user: User, session: AsyncSession,
    settings: Settings, cipher: SecretCipher, redis: Any,
) -> None:
    data = await state.get_data()
    service = _service(session, settings, cipher, user, redis)
    target = event.message if isinstance(event, CallbackQuery) else event
    outcome = await service.prepare(
        _inputs(data, int(data["leverage"])), source=OpeningSource.WIZARD,
        chat_id=target.chat.id if isinstance(target, Message) else None,
    )
    await state.set_state(OpenTradeStates.confirm)
    opening_id = outcome.opening.id if outcome.opening is not None else None
    keyboard = card_keyboard(
        opening_id, can_open=outcome.can_open, warnings=outcome.has_warnings
    )
    sent = await _send(event, state, outcome.text, keyboard)
    if outcome.opening is not None and sent is not None:
        await service.attach_message(outcome.opening, sent.chat.id, sent.message_id)


@router.callback_query(OpenTradeStates.confirm, F.data == OpenCB.EDIT)
@router.callback_query(OpenTradeStates.confirm, F.data == OpenCB.RECALC)
async def edit_card(callback: CallbackQuery, state: FSMContext, user: User,
                    session: AsyncSession, settings: Settings, cipher: SecretCipher,
                    redis: Any) -> None:
    if callback.data == OpenCB.RECALC:
        await _show_card(callback, state, user, session, settings, cipher, redis)
    else:
        await _show_stop(callback, state)


async def _audit(callback: CallbackQuery, db: Database, user: User,
                 action: ExecutionCallbackAction, opening_id: int | None) -> bool:
    message = callback.message if isinstance(callback.message, Message) else None
    try:
        await record_callback(
            db, user_id=user.id, telegram_id=user.telegram_id, action=action,
            notification_id=None, trade_opening_id=opening_id, raw_data=callback.data,
            chat_id=message.chat.id if message else None,
            message_id=message.message_id if message else None, callback_query_id=callback.id,
        )
    except Exception:
        logger.exception("Нажатие кнопки открытия не записано", extra={"user_id": user.id})
        return False
    return True


def _opening_id(data: str | None, prefix: str) -> int | None:
    raw = (data or "").removeprefix(prefix)
    return int(raw) if raw.isdigit() else None


def result_keyboard(outcome: ConfirmOutcome, opening_id: int,
                    symbol: str, side: TradeSide) -> InlineKeyboardMarkup | None:
    if outcome.trade_id is not None:
        return position_keyboard(symbol, side)
    if outcome.status is OpeningStatus.WORKING:
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="✖️ Отменить лимит", callback_data=f"{OpenCB.CANCEL_LIMIT}{opening_id}"
        )]])
    if outcome.status in (OpeningStatus.REFUSED, OpeningStatus.EXPIRED_CARD):
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Пересчитать", callback_data=OpenCB.RECALC)],
            [InlineKeyboardButton(text="◀️ В меню", callback_data=MenuCallback.MAIN)],
        ])
    return None


@router.callback_query(F.data.startswith((OpenCB.YES, OpenCB.YES_WARN)))
async def confirm_open(callback: CallbackQuery, state: FSMContext, user: User,
                       session: AsyncSession, settings: Settings, cipher: SecretCipher,
                       db: Database, redis: Any) -> None:
    warn = str(callback.data).startswith(OpenCB.YES_WARN)
    opening_id = _opening_id(callback.data, OpenCB.YES_WARN if warn else OpenCB.YES)
    action = ExecutionCallbackAction.TO_YES_WARN if warn else ExecutionCallbackAction.TO_YES
    if not await _audit(callback, db, user, action, opening_id):
        await callback.answer(AUDIT_FAILED_TEXT, show_alert=True)
        return
    if opening_id is None or not isinstance(callback.message, Message):
        await callback.answer("Кнопка устарела — открой заново.", show_alert=True)
        return
    await callback.answer("Открываю…")
    service = _service(session, settings, cipher, user, redis)
    outcome = await service.confirm(
        opening_id, accept_warnings=warn, message_id=callback.message.message_id
    )
    if not outcome.final:
        await callback.message.answer(outcome.text)
        return
    opening = await service.load(opening_id)
    keyboard = result_keyboard(
        outcome, opening_id, opening.symbol if opening else "",
        opening.side if opening else TradeSide.LONG,
    )
    if outcome.status in (OpeningStatus.REFUSED, OpeningStatus.EXPIRED_CARD):
        await state.set_state(OpenTradeStates.confirm)   # «Пересчитать» берёт ввод из формы
    else:
        await state.clear()   # переписка мастера удаляется, карточка остаётся итогом
    await callback.message.edit_text(outcome.text, reply_markup=keyboard)


@router.callback_query(F.data.startswith(OpenCB.NO))
async def decline_open(callback: CallbackQuery, state: FSMContext, user: User,
                       session: AsyncSession, settings: Settings, cipher: SecretCipher,
                       db: Database, redis: Any) -> None:
    opening_id = _opening_id(callback.data, OpenCB.NO)
    await _audit(callback, db, user, ExecutionCallbackAction.TO_NO, opening_id)
    text = (
        await _service(session, settings, cipher, user, redis).decline(opening_id)
        if opening_id is not None else "Кнопка устарела."
    )
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="◀️ В меню", callback_data=MenuCallback.MAIN)
            ]]
        ))
    await callback.answer()


@router.callback_query(F.data.startswith(OpenCB.CANCEL_LIMIT))
async def cancel_limit(callback: CallbackQuery, user: User, session: AsyncSession,
                       settings: Settings, cipher: SecretCipher, db: Database,
                       redis: Any) -> None:
    opening_id = _opening_id(callback.data, OpenCB.CANCEL_LIMIT)
    if not await _audit(callback, db, user, ExecutionCallbackAction.TO_CANCEL, opening_id):
        await callback.answer(AUDIT_FAILED_TEXT, show_alert=True)
        return
    if opening_id is None:
        await callback.answer("Кнопка устарела.", show_alert=True)
        return
    await callback.answer("Отменяю лимит…")
    service = _service(session, settings, cipher, user, redis)
    outcome = await service.cancel_limit(opening_id)
    if not isinstance(callback.message, Message):
        return
    if not outcome.final:
        await callback.message.answer(outcome.text)
        return
    opening = await service.load(opening_id)
    keyboard = (
        position_keyboard(opening.symbol, opening.side)
        if outcome.trade_id is not None and opening is not None else None
    )
    await callback.message.edit_text(outcome.text, reply_markup=keyboard)


# --- «Назад» -----------------------------------------------------------------------------------


@router.callback_query(F.data.startswith(OpenCB.BACK))
async def go_back(callback: CallbackQuery, state: FSMContext, user: User,
                  session: AsyncSession, settings: Settings, cipher: SecretCipher,
                  redis: Any) -> None:
    step = str(callback.data).removeprefix(OpenCB.BACK)
    if await state.get_state() is None:
        await callback.answer("Форма закрыта — начни заново.", show_alert=True)
        return
    if step == "mode":
        await show_mode(callback, state)
    elif step == "symbol":
        await _show_symbol(callback, state, user, session)
    elif step == "side":
        await _show_side(callback, state)
    elif step == "type":
        await _show_type(callback, state)
    elif step == "limit":
        await _show_limit_price(callback, state)
    elif step == "expiry":
        await _show_expiry(callback, state, settings)
    elif step == "stop":
        await _show_stop(callback, state)
    elif step == "take":
        await _show_take(callback, state)
    elif step == "risk":
        await _show_risk(callback, state, user, session)
    else:
        await callback.answer("Кнопка устарела.", show_alert=True)
