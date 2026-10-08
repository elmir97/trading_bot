"""Кнопки действий с позицией (этап 4): карточка подтверждения и «Да».

Кнопки — на экране действий позиции («Позиции» → «⚙️ XRP LONG»,
app/bot/handlers/positions.py):
pa:{be|sl|tp|c25|c50|cf}:{SYMBOL}:{L|S}. Стоп/тейк на свою цену — ввод
числом (FSM). Карточка — pm:y:{id} «Да», pm:r:{id} «⚠️ Да, увеличить риск»,
pm:n:{id} «Нет»; id — position_actions.id (снимок карточки).

Каждое нажатие — строка журнала нажатий (execution_callbacks, pm_*) первым
действием, из своей сессии. Сбой записи на «Да» блокирует отправку, как у
входа по сигналу (префлайт 15.7). Расчёт и отправка —
app/execution/position_action_service.py.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import outbox
from app.bot.handlers.exchange import _describe, _market_cache
from app.bot.keyboards.main import MenuCallback
from app.bot.messaging import edit_or_replace
from app.bot.prompts import ask_number
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.numfmt import fmt_price
from app.core.security import SecretCipher
from app.database.models.outgoing_message import OutgoingMeta
from app.database.models.position_action import PositionAction
from app.database.models.user import User
from app.database.session import Database
from app.exchanges.base import ExchangeError
from app.execution.callback_audit import record_callback
from app.execution.position_action_service import PositionActionService
from app.market.data import MarketDataService
from app.services.exchange_factory import ExchangeFactory
from app.trading.enums import ExecutionCallbackAction, PositionActionKind, TradeSide

router = Router(name="position_actions")
logger = get_logger(__name__)

AUDIT_FAILED_TEXT = (
    "Не удалось записать нажатие — действие не выполнено, на биржу ничего не "
    "отправлено. Попробуй ещё раз."
)

SIDE_BY_CODE = {"L": TradeSide.LONG, "S": TradeSide.SHORT}
SIDE_CODE = {TradeSide.LONG: "L", TradeSide.SHORT: "S"}

# Код кнопки → (действие, параметры). sl/tp — сначала ввод цены.
_DIRECT: dict[str, tuple[PositionActionKind, dict[str, object]]] = {
    "be": (PositionActionKind.MOVE_STOP, {"breakeven": True}),
    "c25": (PositionActionKind.CLOSE_PARTIAL, {"fraction": "25"}),
    "c50": (PositionActionKind.CLOSE_PARTIAL, {"fraction": "50"}),
    "cf": (PositionActionKind.CLOSE_FULL, {}),
}
_PROMPT: dict[str, tuple[PositionActionKind, str]] = {
    "sl": (PositionActionKind.MOVE_STOP, "стопа"),
    "tp": (PositionActionKind.SET_TAKE, "тейка"),
}


class ActionCB:
    OPEN = "pa:"
    YES = "pm:y:"
    YES_RISK = "pm:r:"
    NO = "pm:n:"
    CANCEL_INPUT = "pc:x"   # отмена ввода цены стопа/тейка (03.10)


class PositionActionStates(StatesGroup):
    price = State()


def action_buttons(symbol: str, side: TradeSide) -> list[tuple[str, str]]:
    """Подписи и callback_data кнопок экрана действий одной позиции
    (app/bot/handlers/positions.py, «⚙️ XRP LONG»): позиция — в заголовке
    экрана, подписи без символа."""
    tail = f"{symbol}:{SIDE_CODE[side]}"
    return [
        ("🛡 Стоп в безубыток", f"{ActionCB.OPEN}be:{tail}"),
        ("✏️ Изменить стоп", f"{ActionCB.OPEN}sl:{tail}"),
        ("🎯 Тейк", f"{ActionCB.OPEN}tp:{tail}"),
        ("✂️ Закрыть 25%", f"{ActionCB.OPEN}c25:{tail}"),
        ("✂️ Закрыть 50%", f"{ActionCB.OPEN}c50:{tail}"),
        ("❌ Закрыть всё", f"{ActionCB.OPEN}cf:{tail}"),
    ]


def card_keyboard(action_id: int, risk_increase: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if risk_increase:
        builder.button(text="⚠️ Да, увеличить риск", callback_data=f"{ActionCB.YES_RISK}{action_id}")
    else:
        builder.button(text="✅ Да", callback_data=f"{ActionCB.YES}{action_id}")
    builder.button(text="❌ Нет", callback_data=f"{ActionCB.NO}{action_id}")
    builder.adjust(2)
    return builder.as_markup()


def back_to_positions() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ К позициям", callback_data=MenuCallback.OPEN_POSITIONS)
    builder.button(text="◀️ В меню", callback_data=MenuCallback.MAIN)
    builder.adjust(2)
    return builder.as_markup()


def cancel_input_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✖️ Отмена", callback_data=ActionCB.CANCEL_INPUT)
    builder.button(text="⬅️ К позициям", callback_data=MenuCallback.OPEN_POSITIONS)
    builder.adjust(2)
    return builder.as_markup()


def example_level(mark: Decimal, side: TradeSide, is_stop: bool, precision: int) -> Decimal:
    """Пример для подсказки ввода: стоп в 2% от цены в сторону убытка,
    тейк — в сторону прибыли; округление до шага цены символа."""
    below = is_stop == (side is TradeSide.LONG)
    factor = Decimal("0.98") if below else Decimal("1.02")
    return (mark * factor).quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP)


async def _placeholder(
    settings: Settings, cipher: SecretCipher, symbol: str, side: TradeSide, is_stop: bool
) -> str:
    what = "Цена стопа" if is_stop else "Цена тейка"
    try:
        client = ExchangeFactory(settings, cipher).public_client()
        try:
            market = MarketDataService(client, _market_cache)
            mark = (await market.get_mark_prices([symbol])).get(symbol)
            info = next((i for i in await market.get_symbols() if i.symbol == symbol), None)
        finally:
            await client.close()
    except Exception:
        logger.warning("Подсказка ввода: цена не получена", extra={"symbol": symbol})
        return f"{what} числом"
    if mark is None or info is None:
        return f"{what} числом"
    example = example_level(mark, side, is_stop, info.price_precision)
    return f"{what}, например {fmt_price(example, info.price_precision)}"


def _parse_open(data: str) -> tuple[str, str, TradeSide] | None:
    parts = data.removeprefix(ActionCB.OPEN).split(":")
    if len(parts) != 3 or parts[2] not in SIDE_BY_CODE:
        return None
    return parts[0], parts[1], SIDE_BY_CODE[parts[2]]


def _service(
    session: AsyncSession, settings: Settings, cipher: SecretCipher, user: User, redis: Any
) -> PositionActionService:
    return PositionActionService(
        session, settings, cipher, user, redis=redis, market_cache=_market_cache
    )


async def _audit(
    db: Database, user: User, action: ExecutionCallbackAction, callback: CallbackQuery,
    position_action_id: int | None,
) -> bool:
    message = callback.message if isinstance(callback.message, Message) else None
    try:
        await record_callback(
            db, user_id=user.id, telegram_id=user.telegram_id, action=action,
            notification_id=None, position_action_id=position_action_id,
            raw_data=callback.data, chat_id=message.chat.id if message else None,
            message_id=message.message_id if message else None,
            callback_query_id=callback.id,
        )
    except Exception:
        logger.exception("Нажатие кнопки действия не записано", extra={"user_id": user.id})
        return False
    return True


async def _show_card(
    target: Message, service: PositionActionService, kind: PositionActionKind,
    params: dict[str, object], symbol: str, side: TradeSide, *, edit: bool,
) -> None:
    try:
        outcome = await service.open_card(
            kind, params, symbol, side, message_id=target.message_id if edit else None
        )
    except ExchangeError as exc:
        logger.warning("Карточка действия: биржа не ответила", extra={"symbol": symbol})
        text, keyboard = _describe(exc), back_to_positions()
        if edit:
            await edit_or_replace(target, text, keyboard)
        else:
            await target.answer(text, reply_markup=keyboard)
        return
    action = outcome.action
    keyboard = (
        back_to_positions() if outcome.refused or action is None
        else card_keyboard(action.id, outcome.risk_increase)
    )
    if edit:
        await edit_or_replace(target, outcome.text, keyboard)
        return
    sent = await target.answer(outcome.text, reply_markup=keyboard)
    if action is not None and not outcome.refused:
        # Карточка новым сообщением (после ввода цены) — «Да» сверяет его id.
        await service.attach_message(action, sent.message_id)


@router.callback_query(F.data.startswith(ActionCB.OPEN))
async def open_action(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User,
    settings: Settings, cipher: SecretCipher, db: Database, redis: Any,
) -> None:
    parsed = _parse_open(str(callback.data))
    if parsed is None or not isinstance(callback.message, Message):
        await callback.answer("Кнопка устарела — открой «Позиции» заново.", show_alert=True)
        return
    code, symbol, side = parsed
    await _audit(db, user, ExecutionCallbackAction.PM_OPEN, callback, None)
    if code in _PROMPT:
        kind, what = _PROMPT[code]
        await state.set_state(PositionActionStates.price)
        await state.update_data(kind=kind.value, symbol=symbol, side=side.value)
        await callback.answer()
        # 03.10: вопрос — отдельным сообщением с ForceReply (app/bot/prompts):
        # фоновые уведомления не уводят его из-под ответа.
        await edit_or_replace(
            callback.message,
            f"⏳ Жду цену {what} для <b>{symbol} {side.value}</b> — ответь числом на "
            "сообщение ниже (стоп и тейк ставятся на всю позицию).",
            cancel_input_keyboard(),
        )
        placeholder = await _placeholder(
            settings, cipher, symbol, side, kind is PositionActionKind.MOVE_STOP
        )
        await ask_number(callback, state, placeholder)
        return
    if code not in _DIRECT:
        await callback.answer("Неизвестное действие.", show_alert=True)
        return
    await callback.answer("Считаю…")
    kind, params = _DIRECT[code]
    await _show_card(
        callback.message, _service(session, settings, cipher, user, redis),
        kind, params, symbol, side, edit=True,
    )


@router.callback_query(F.data == ActionCB.CANCEL_INPUT)
async def cancel_input(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена ввода цены: состояние снимается (вопрос с ForceReply удалит
    PromptMiddleware), на биржу ничего не отправлено."""
    await state.clear()
    await callback.answer()
    if isinstance(callback.message, Message):
        await edit_or_replace(
            callback.message, "Отменено — на биржу ничего не отправлено.", back_to_positions()
        )


@router.message(PositionActionStates.price)
async def price_entered(
    message: Message, state: FSMContext, session: AsyncSession, user: User,
    settings: Settings, cipher: SecretCipher, redis: Any,
) -> None:
    # 03.10: сначала проверка ввода, потом снятие состояния. Не число —
    # ответ, состояние остаётся, PromptMiddleware задаёт вопрос заново
    # (раньше «Абв» сбрасывало ввод и превращалось в карточку-отказ).
    raw = (message.text or "").strip().replace(",", ".")
    try:
        level = Decimal(raw)
    except InvalidOperation:
        level = None
    if level is None or not level.is_finite() or level <= 0:
        await message.answer("Не понял — нужна цена числом, например 1.4850.")
        return
    data = await state.get_data()
    await state.clear()
    kind = PositionActionKind(data["kind"])
    await _show_card(
        message, _service(session, settings, cipher, user, redis), kind,
        {"level": raw}, data["symbol"], TradeSide(data["side"]),
        edit=False,
    )


@router.callback_query(F.data.startswith((ActionCB.YES, ActionCB.YES_RISK, ActionCB.NO)))
async def decide(
    callback: CallbackQuery, session: AsyncSession, user: User, settings: Settings,
    cipher: SecretCipher, db: Database, redis: Any,
) -> None:
    data = str(callback.data)
    prefix = next(p for p in (ActionCB.YES_RISK, ActionCB.YES, ActionCB.NO) if data.startswith(p))
    try:
        action_id = int(data.removeprefix(prefix))
    except ValueError:
        await callback.answer("Кнопка устарела.", show_alert=True)
        return
    audit_action = {
        ActionCB.YES: ExecutionCallbackAction.PM_YES,
        ActionCB.YES_RISK: ExecutionCallbackAction.PM_YES_RISK,
        ActionCB.NO: ExecutionCallbackAction.PM_NO,
    }[prefix]
    recorded = await _audit(db, user, audit_action, callback, action_id)
    service = _service(session, settings, cipher, user, redis)
    message = callback.message if isinstance(callback.message, Message) else None
    if prefix == ActionCB.NO:
        text = await service.decline(action_id)
        await callback.answer()
        if message is not None:
            await edit_or_replace(message, text, back_to_positions())
        return
    if not recorded:
        await callback.answer(AUDIT_FAILED_TEXT, show_alert=True)
        return
    try:
        outcome = await service.confirm(
            action_id, message_id=message.message_id if message else None,
            risk_confirmed=prefix == ActionCB.YES_RISK,
        )
    except ExchangeError as exc:
        await callback.answer()
        if message is not None:
            await edit_or_replace(message, _describe(exc), back_to_positions())
        return
    if not outcome.final:
        await callback.answer(outcome.text, show_alert=True)
        return
    await callback.answer()
    if message is not None:
        meta = OutgoingMeta(
            user_id=user.id, kind="ACTION_RESULT", position_action_id=action_id
        )
        await outbox.edit(message, db, meta, outcome.text, back_to_positions())


async def card_action(session: AsyncSession, action_id: int) -> PositionAction | None:
    """Для тестов и smoke: строка действия по id."""
    return await session.get(PositionAction, action_id)
