"""Карточка подтверждения входа по сигналу (этап 15.4, разделы 1 и 5 ТЗ).

Хендлер только собирает контекст и рисует кнопки — вся торговая логика
(guards → sizing → OrderRequest) живёт в app/execution/service.py, сюда
не переносится. Отправки ордера здесь нет и не будет: при «Да» — только
запись в execution_orders со статусом DRY_RUN и сообщение с полным
содержимым OrderRequest (появится в 15.5).

Состояние карточки (цена на момент показа, TTL) живёт в памяти процесса,
как и _market_cache в exchange.py — рестарт бота теряет незавершённые
карточки, но не позиции: ни одна не создана, пока не подтверждена. Это
тот же компромисс, что и у остальных кэшей в проекте.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.exchange import _describe, _market_cache
from app.bot.keyboards.execution import ExecutionCB, confirm_keyboard, expired_keyboard
from app.bot.keyboards.trade import fmt_amount, fmt_num, fmt_qty
from app.core.config import Settings
from app.core.locks import LockBusyError, RedisLike, RedisLock, confirm_lock_key
from app.core.logging import get_logger
from app.core.security import SecretCipher
from app.database.models.signal import SignalRecord
from app.database.models.trading_plan import TradingPlan
from app.database.models.user import User
from app.database.repositories.execution_order import ExecutionOrderRepository
from app.database.repositories.signal import SignalRepository
from app.exchanges.base import ExchangeAuthError, ExchangeError
from app.execution.models import ExecutionRefusal, ExecutionRefusalCode
from app.execution.service import ExecutionQuote, ExecutionService, build_execution_orders
from app.market.data import MarketDataService
from app.services.exchange_factory import ExchangeFactory
from app.trading.enums import OrderStatus, SignalLevel, SignalRecordStatus

router = Router(name="execution")
logger = get_logger(__name__)

_EvaluationResult = ExecutionQuote | ExecutionRefusal | str


@dataclass(slots=True)
class _ConfirmationState:
    """Снимок карточки между показом и «Да» — источник planned_price для
    дрейф-проверки (раздел 5 ТЗ) и якорь TTL."""

    planned_price: Decimal
    created_at: datetime
    chat_id: int
    message_id: int


# (user_id, signal_id) → активная карточка. Одна на пару: новая карточка
# (в т.ч. пересчёт после PRICE_DRIFT) вытесняет предыдущую.
_confirmations: dict[tuple[int, int], _ConfirmationState] = {}

# Сильные ссылки на фоновые задачи TTL: без этого asyncio может собрать
# задачу сборщиком мусора до того, как она успеет доспать (RUF006).
_background_tasks: set[asyncio.Task[None]] = set()


def _parse_signal_id(data: str | None, prefix: str) -> int | None:
    if data is None:
        return None
    try:
        return int(data.removeprefix(prefix))
    except ValueError:
        return None


def render_refusal(refusal: ExecutionRefusal) -> str:
    """Раздел 12 ТЗ: человеческим текстом, без stack trace."""
    return f"🚫 Не открыл: {refusal.message}"


def _signal_reference_price(signal: SignalRecord) -> Decimal | None:
    if signal.entry_low is not None and signal.entry_high is not None:
        return (signal.entry_low + signal.entry_high) / 2
    return signal.entry_low or signal.entry_high


def render_confirmation(quote: ExecutionQuote, signal: SignalRecord, settings: Settings) -> str:
    """Раздел 5 ТЗ: карточка подтверждения со всеми цифрами."""
    order = quote.order
    base_asset = order.symbol.split("-")[0]

    drift_note = ""
    reference = _signal_reference_price(signal)
    if reference is not None and reference > 0:
        drift_pct = abs(order.entry_price - reference) / reference * Decimal(100)
        drift_note = (
            f" (сигнал был на {fmt_num(reference)}, "
            f"дрейф {fmt_num(drift_pct.quantize(Decimal('0.01')))}%)"
        )

    stop_pct = abs(order.entry_price - order.stop_loss) / order.entry_price * Decimal(100)

    lines = [
        f"<b>{order.symbol} · {order.position_side.label} · маркет</b>",
        f"Цена сейчас: {fmt_num(order.entry_price)}{drift_note}",
        f"Объём: {fmt_qty(order.quantity)} {base_asset} ≈ "
        f"{fmt_amount(order.notional)} USDT нотионал",
        f"Плечо: {order.leverage}x, маржа {fmt_amount(order.margin)} USDT",
        f"Стоп: {fmt_num(order.stop_loss)}  (−{fmt_num(stop_pct.quantize(Decimal('0.01')))}%)  "
        f"риск {fmt_amount(order.risk_amount)} USDT = {fmt_num(order.risk_percent)}% депозита",
        f"Тейк: {fmt_num(order.take_profit)}  RR 1:{fmt_num(order.risk_reward)}",
        "",
        f"Открытых позиций сейчас: {quote.open_positions_count} из "
        f"{settings.exec_max_open_positions}",
        f"Суммарный риск после входа: {fmt_num(quote.total_risk_after_percent)}% из "
        f"{fmt_num(settings.exec_max_total_risk_percent)}%",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Сбор контекста и вызов ExecutionService
# ---------------------------------------------------------------------------


async def _build_quote(
    session: AsyncSession,
    user: User,
    signal: SignalRecord,
    plan: TradingPlan,
    settings: Settings,
    cipher: SecretCipher,
    *,
    planned_price: Decimal | None,
) -> _EvaluationResult:
    """guards.NO_TRADING_KEY срабатывает до сетевого похода на биржу — поэтому
    без ключа безопасно использовать публичный клиент: ExecutionService.evaluate()
    вернёт отказ раньше, чем попытается что-то запросить приватным методом."""
    factory = ExchangeFactory(settings, cipher)
    credentials = await factory.get_credentials(session, user.id)
    has_trading_key = credentials is not None
    key_can_trade_futures = has_trading_key and not credentials.is_read_only  # type: ignore[union-attr]

    if has_trading_key:
        try:
            client = await factory.for_user(session, user.id)
        except ExchangeAuthError as exc:
            return _describe(exc)
    else:
        client = factory.public_client()

    market = MarketDataService(client, _market_cache)
    service = ExecutionService(session=session, settings=settings, client=client, market=market)
    try:
        return await service.evaluate(
            user=user,
            signal=signal,
            plan=plan,
            has_trading_key=has_trading_key,
            key_can_trade_futures=key_can_trade_futures,
            planned_price=planned_price,
        )
    except ExchangeError as exc:
        logger.warning("Биржа недоступна при оценке исполнения", extra={"user_id": user.id})
        return _describe(exc)
    finally:
        await client.close()


async def _send_result(
    bot: Bot,
    chat_id: int,
    user: User,
    signal: SignalRecord,
    result: _EvaluationResult,
    settings: Settings,
) -> None:
    if isinstance(result, str):
        await bot.send_message(chat_id, result)
        return
    if isinstance(result, ExecutionRefusal):
        await bot.send_message(chat_id, render_refusal(result))
        return

    text = render_confirmation(result, signal, settings)
    sent = await bot.send_message(chat_id, text, reply_markup=confirm_keyboard(signal.id))
    now = datetime.now(UTC)
    _confirmations[(user.id, signal.id)] = _ConfirmationState(
        planned_price=result.order.entry_price,
        created_at=now,
        chat_id=sent.chat.id,
        message_id=sent.message_id,
    )
    task = asyncio.create_task(
        _expire_card(
            bot, user.id, signal.id, sent.chat.id, sent.message_id,
            settings.exec_confirm_ttl_seconds,
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _expire_card(
    bot: Bot, user_id: int, signal_id: int, chat_id: int, message_id: int, ttl_seconds: int
) -> None:
    """Раздел 5 ТЗ: по истечении TTL кнопки заменяются на «просрочено».

    Сверка по message_id перед правкой: если карточку уже обработали
    (Да/Нет) или заменили пересчитанной после PRICE_DRIFT, задача не
    трогает чужое сообщение — просто выходит.
    """
    await asyncio.sleep(ttl_seconds)
    key = (user_id, signal_id)
    state = _confirmations.get(key)
    if state is None or state.chat_id != chat_id or state.message_id != message_id:
        return
    del _confirmations[key]
    with contextlib.suppress(TelegramBadRequest):
        # Сообщение могли удалить/отредактировать руками — не критично.
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=expired_keyboard()
        )


# ---------------------------------------------------------------------------
# Кнопки
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith(ExecutionCB.OPEN))
async def open_confirmation(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    settings: Settings,
    cipher: SecretCipher,
) -> None:
    signal_id = _parse_signal_id(str(callback.data), ExecutionCB.OPEN)
    await callback.answer()
    if signal_id is None or not isinstance(callback.message, Message) or callback.bot is None:
        return

    signal = await SignalRepository(session).get(signal_id, user.id)
    if (
        signal is None
        or signal.level is not SignalLevel.READY
        or signal.status is not SignalRecordStatus.ACTIVE
    ):
        await callback.message.answer("⏳ Сигнал уже не актуален.")
        return

    plan = user.trading_plan
    if plan is None:
        await callback.message.answer("Сначала настрой торговый план в «Настройках».")
        return

    result = await _build_quote(
        session, user, signal, plan, settings, cipher, planned_price=None
    )
    await _send_result(callback.bot, callback.message.chat.id, user, signal, result, settings)


@router.callback_query(F.data.startswith(ExecutionCB.NO))
async def confirm_no(callback: CallbackQuery, user: User) -> None:
    signal_id = _parse_signal_id(str(callback.data), ExecutionCB.NO)
    await callback.answer()
    if signal_id is None or not isinstance(callback.message, Message):
        return
    _confirmations.pop((user.id, signal_id), None)
    await callback.message.edit_text("❌ Вход отменён.", reply_markup=None)


@router.callback_query(F.data == ExecutionCB.EXPIRED)
async def expired_noop(callback: CallbackQuery) -> None:
    await callback.answer("Карточка устарела. Дождись нового сигнала.", show_alert=True)


@router.callback_query(F.data.startswith(ExecutionCB.YES))
async def confirm_yes(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    settings: Settings,
    cipher: SecretCipher,
    redis: RedisLike,
) -> None:
    signal_id = _parse_signal_id(str(callback.data), ExecutionCB.YES)
    if signal_id is None:
        await callback.answer()
        return

    # Раздел 8 ТЗ: защита от двойного нажатия. Занятый ключ — мгновенный
    # ответ "уже обрабатывается", без ожидания и без повторной попытки.
    try:
        async with RedisLock(redis, confirm_lock_key(user.id, signal_id), ttl_seconds=15):
            await _process_confirm(callback, session, user, signal_id, settings, cipher)
    except LockBusyError:
        await callback.answer("Уже обрабатывается…", show_alert=True)


async def _process_confirm(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    signal_id: int,
    settings: Settings,
    cipher: SecretCipher,
) -> None:
    await callback.answer()
    if not isinstance(callback.message, Message) or callback.bot is None:
        return

    key = (user.id, signal_id)
    state = _confirmations.get(key)
    now = datetime.now(UTC)
    ttl = timedelta(seconds=settings.exec_confirm_ttl_seconds)

    if (
        state is None
        or state.message_id != callback.message.message_id
        or now - state.created_at > ttl
    ):
        # Либо TTL истёк раньше фоновой правки кнопок, либо это устаревшая
        # карточка (заменена пересчётом после PRICE_DRIFT), либо бот
        # перезапускался — во всех случаях тихий вход недопустим.
        _confirmations.pop(key, None)
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_reply_markup(reply_markup=expired_keyboard())
        return

    signal = await SignalRepository(session).get(signal_id, user.id)
    if (
        signal is None
        or signal.level is not SignalLevel.READY
        or signal.status is not SignalRecordStatus.ACTIVE
    ):
        _confirmations.pop(key, None)
        await callback.message.edit_text("⏳ Сигнал уже не актуален.", reply_markup=None)
        return

    plan = user.trading_plan
    if plan is None:
        _confirmations.pop(key, None)
        await callback.message.edit_text(
            "Сначала настрой торговый план в «Настройках».", reply_markup=None
        )
        return

    result = await _build_quote(
        session, user, signal, plan, settings, cipher, planned_price=state.planned_price
    )

    if isinstance(result, str):
        _confirmations.pop(key, None)
        await callback.message.edit_text(result, reply_markup=None)
        return

    if isinstance(result, ExecutionRefusal):
        _confirmations.pop(key, None)
        if result.code is ExecutionRefusalCode.PRICE_DRIFT:
            # Раздел 5 ТЗ: не тихий вход по новой цене — новая карточка
            # с пересчётом и повторным "Да/Нет".
            await callback.message.edit_reply_markup(reply_markup=None)
            new_result = await _build_quote(
                session, user, signal, plan, settings, cipher, planned_price=None
            )
            await callback.message.answer(
                "↻ Цена ушла дальше допустимого. Пересчитал карточку:"
            )
            await _send_result(
                callback.bot, callback.message.chat.id, user, signal, new_result, settings
            )
        else:
            await callback.message.edit_text(render_refusal(result), reply_markup=None)
        return

    # Успех: этап 15.4 — реального ордера не будет, только сухой прогон.
    _confirmations.pop(key, None)
    order = result.order

    orders_repo = ExecutionOrderRepository(session)
    for row in build_execution_orders(order, OrderStatus.DRY_RUN):
        orders_repo.add(row)
    await orders_repo.flush()

    # Тот же guard SIGNAL_ALREADY_USED (раздел 7, п.4), что и для реального
    # входа: повторное подтверждение того же сигнала после сухого прогона
    # тоже должно быть отклонено, а не превращаться во вторую запись.
    signal.trade_opened_at = now
    await session.flush()

    logger.info(
        "Сухой прогон исполнения: ушёл бы ордер",
        extra={"user_id": user.id, "signal_id": signal.id, "order": order.render()},
    )

    await callback.message.edit_text(
        "✅ Подтверждено (сухой прогон — реального ордера нет)", reply_markup=None
    )
    await callback.message.answer(
        f"🧪 <b>Сухой прогон: ушёл бы такой ордер</b>\n\n<code>{order.render()}</code>"
    )
