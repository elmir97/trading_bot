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

Раздел 12а ТЗ: каждый исход попытки входа по READY-сигналу пишет строку в
execution_orders — DRY_RUN при «Да», DECLINED при «Нет», EXPIRED по TTL.
Отказ гварда (REFUSED) пишет сам ExecutionService.evaluate(), не здесь.
Отдельной таблицы под ежедневную сводку исполнения нет — см. раздел
"Что накапливать" в ТЗ и app/workers/execution_digest.py.
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.formatting import fmt_amount, fmt_price, fmt_qty, fmt_ratio
from app.bot.handlers.exchange import _describe, _market_cache
from app.bot.keyboards.execution import ExecutionCB, confirm_keyboard, expired_keyboard
from app.core.config import Settings
from app.core.locks import LockBusyError, RedisLike, RedisLock, confirm_lock_key
from app.core.logging import get_logger
from app.core.security import SecretCipher
from app.database.models.signal import SignalRecord
from app.database.models.trading_plan import TradingPlan
from app.database.models.user import User
from app.database.repositories.execution_order import ExecutionOrderRepository
from app.database.repositories.signal import SignalRepository
from app.database.session import Database
from app.exchanges.base import ExchangeAuthError, ExchangeError
from app.execution.models import ExecutionRefusal, ExecutionRefusalCode
from app.execution.service import (
    ExecutionQuote,
    ExecutionService,
    build_exchange_error_order,
    build_execution_orders,
    build_observation_order_from_quote,
    price_drift_percent,
    signal_reference_price,
)
from app.market.cache import TTLCache
from app.market.data import MarketDataService
from app.services.exchange_factory import ExchangeFactory
from app.services.permissions import refresh_permissions
from app.services.position_mode import refresh_position_mode
from app.trading.enums import ObservationStage, OrderStatus, SignalLevel, SignalRecordStatus

router = Router(name="execution")
logger = get_logger(__name__)

_EvaluationResult = ExecutionQuote | ExecutionRefusal | str


@dataclass(slots=True)
class _ConfirmationState:
    """Снимок карточки между показом и «Да» — источник planned_price для
    дрейф-проверки (раздел 5 ТЗ) и якорь TTL.

    quote/drift_percent — раздел 12а ТЗ: то же самое, что показано на
    карточке, нужно, чтобы записать исход (DECLINED/EXPIRED) с теми же
    числами, если пользователь ответит «Нет» или карточка просто истечёт —
    без повторного похода на биржу ради чисел, которые уже посчитаны.
    """

    planned_price: Decimal
    created_at: datetime
    chat_id: int
    message_id: int
    quote: ExecutionQuote
    drift_percent: Decimal | None


# (user_id, signal_id) → активная карточка. Одна на пару: новая карточка
# (в т.ч. пересчёт после PRICE_DRIFT) вытесняет предыдущую.
_confirmations: dict[tuple[int, int], _ConfirmationState] = {}

# Сильные ссылки на фоновые задачи TTL: без этого asyncio может собрать
# задачу сборщиком мусора до того, как она успеет доспать (RUF006).
_background_tasks: set[asyncio.Task[None]] = set()

# Раздел 16 ТЗ, шаг 15.5.1: режим позиций — приватные, подписанные данные
# аккаунта, отдельный кэш от _market_cache (тот публичный и общий на
# процесс по конструкции) — см. app/services/position_mode.py.
_position_mode_cache = TTLCache()


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


def render_confirmation(quote: ExecutionQuote, signal: SignalRecord, settings: Settings) -> str:
    """Раздел 5 ТЗ: карточка подтверждения со всеми цифрами."""
    order = quote.order
    base_asset = order.symbol.split("-")[0]
    price_precision = quote.symbol_info.price_precision
    quantity_precision = quote.symbol_info.quantity_precision

    drift_note = ""
    reference = signal_reference_price(signal)
    drift_pct = price_drift_percent(order.entry_price, signal)
    if reference is not None and drift_pct is not None:
        drift_note = (
            f" (сигнал был на {fmt_price(reference, price_precision)}, "
            f"дрейф {fmt_ratio(drift_pct)}%)"
        )

    stop_pct = abs(order.entry_price - order.stop_loss) / order.entry_price * Decimal(100)

    lines = [
        f"<b>{order.symbol} · {order.position_side.label} · маркет</b>",
        f"Цена сейчас: {fmt_price(order.entry_price, price_precision)}{drift_note}",
        f"Объём: {fmt_qty(order.quantity, quantity_precision)} {base_asset} ≈ "
        f"{fmt_amount(order.notional)} USDT нотионал",
        f"Плечо: {order.leverage}x, маржа {fmt_amount(order.margin)} USDT",
        f"Стоп: {fmt_price(order.stop_loss, price_precision)}  (−{fmt_ratio(stop_pct)}%)  "
        f"риск {fmt_amount(order.risk_amount)} USDT = {fmt_ratio(order.risk_percent)}% депозита",
        f"Тейк: {fmt_price(order.take_profit, price_precision)}  "
        f"RR 1:{fmt_ratio(order.risk_reward)}",
        "",
        f"Открытых позиций сейчас: {quote.open_positions_count} из "
        f"{settings.exec_max_open_positions}",
        f"Суммарный риск после входа: {fmt_ratio(quote.total_risk_after_percent)}% из "
        f"{fmt_ratio(settings.exec_max_total_risk_percent)}%",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Сбор контекста и вызов ExecutionService
# ---------------------------------------------------------------------------

# Раздел 8 ТЗ: имя UNIQUE-констрейнта на execution_orders.client_order_id
# (см. alembic/versions/ddace081d9fb_execution_orders.py). Это единственная
# настоящая гарантия единственности входа при гонке двух параллельных
# _process_confirm на один сигнал (например, если Redis-лок протух посреди
# легитимной обработки — TTL лока лишь предохранитель, не гарантия, см.
# Settings.confirm_lock_ttl_seconds). Redis-лок сам по себе даёт только
# быстрый отказ при обычном двойном тапе, пока он ещё держится.
_CLIENT_ORDER_ID_UNIQUE_CONSTRAINT = "uq_execution_orders_client_order_id"


def _is_client_order_id_collision(exc: IntegrityError) -> bool:
    """True — это гонка по client_order_id (ожидаемо, ловим и отвечаем
    по-человечески). False — IntegrityError другого происхождения (NOT
    NULL, FK и т.п.) — это баг кода, не гонка, пробрасывать дальше как
    есть, а не маскировать текстом «уже обрабатывается».

    exc.orig — не сам asyncpg-эксепшен, а обёртка SQLAlchemy
    (AsyncAdapt_asyncpg_dbapi.IntegrityError) для совместимости с
    DBAPI-интерфейсом; у неё самой .constraint_name нет. Реальный
    asyncpg.exceptions.UniqueViolationError/ForeignKeyViolationError/… —
    в exc.orig.__cause__, только там есть .constraint_name (проверено
    живым запросом к тестовой БД, не по документации asyncpg)."""
    cause = getattr(exc.orig, "__cause__", None)
    return getattr(cause, "constraint_name", None) == _CLIENT_ORDER_ID_UNIQUE_CONSTRAINT


async def _record_exchange_error(
    session: AsyncSession,
    user: User,
    signal: SignalRecord,
    error: ExchangeError,
    *,
    at_confirm: bool,
) -> None:
    """Раздел 12а: сбой биржи на пути входа не теряется — строка ERROR с
    классом исключения. Запись идёт в SAVEPOINT и не должна ронять ответ
    пользователю: если БД недоступна, текст об ошибке биржи всё равно уйдёт,
    а сбой записи — в лог (не молча)."""
    if signal.direction is None:
        logger.error(
            "Сбой биржи по сигналу без направления — наблюдение не записано",
            extra={"user_id": user.id, "signal_id": signal.id},
        )
        return
    stage = ObservationStage.CONFIRM if at_confirm else ObservationStage.CARD
    try:
        async with session.begin_nested():
            session.add(
                build_exchange_error_order(
                    user_id=user.id,
                    signal_id=signal.id,
                    symbol=signal.symbol,
                    direction=signal.direction,
                    error=error,
                    stage=stage,
                )
            )
    except Exception:
        logger.exception(
            "Не удалось записать сбой биржи в execution_orders",
            extra={"user_id": user.id, "signal_id": signal.id},
        )


async def _build_quote(
    session: AsyncSession,
    user: User,
    signal: SignalRecord,
    plan: TradingPlan,
    settings: Settings,
    cipher: SecretCipher,
    *,
    planned_price: Decimal | None,
    check_permissions: bool,
    known_dual_side_position: bool | None = None,
) -> _EvaluationResult:
    """guards.NO_TRADING_KEY срабатывает до сетевого похода на биржу — поэтому
    без ключа безопасно использовать публичный клиент: ExecutionService.evaluate()
    вернёт отказ раньше, чем попытается что-то запросить приватным методом.

    Этап 15.4в: ключ и клиент берутся для режима, разрешённого КОНФИГОМ
    (settings.bingx_allowed_exchange_mode) — туда реально ушёл бы ордер,
    а не для того, что выбрано в настройках на показ. guards.MODE_NOT_ALLOWED
    сверяет это с user.settings.active_exchange_mode и отказывает при
    расхождении раньше, чем дело дойдёт до цены/баланса (см. service.py).

    check_permissions: раздел 8 ТЗ — обязан быть False на вызовах внутри
    RedisLock (confirm_yes/"Да"): лишний поход на биржу там не нужен, права
    уже проверены при показе карточки. True только при первом построении
    карточки (open_confirmation).

    known_dual_side_position: раздел 16 ТЗ, шаг 15.5.1 — тот же принцип,
    что и check_permissions, но для режима позиций: на «Да» не
    перезапрашиваем, несём значение из ExecutionQuote карточки, которая
    уже была показана (state.quote.dual_side_position в _process_confirm).
    При check_permissions=True игнорируется — там читаем заново."""
    allowed_mode = settings.bingx_allowed_exchange_mode
    selected_mode = user.settings.active_exchange_mode

    factory = ExchangeFactory(settings, cipher)
    credentials = await factory.get_credentials(session, user.id, mode=allowed_mode)
    has_trading_key = credentials is not None
    key_can_trade_futures = has_trading_key and not credentials.is_read_only  # type: ignore[union-attr]

    if has_trading_key:
        try:
            client = await factory.for_user(session, user.id, mode=allowed_mode)
        except ExchangeAuthError as exc:
            await _record_exchange_error(
                session, user, signal, exc, at_confirm=planned_price is not None
            )
            return _describe(exc)
    else:
        client = factory.public_client()

    permissions_trustworthy = True
    if has_trading_key and check_permissions:
        outcome = await refresh_permissions(
            session, credentials, client, ttl_hours=settings.exec_permissions_ttl_hours  # type: ignore[arg-type]
        )
        permissions_trustworthy = outcome.trustworthy
        if outcome.trustworthy:
            key_can_trade_futures = not credentials.is_read_only  # type: ignore[union-attr]

    dual_side_position = known_dual_side_position
    if has_trading_key and check_permissions:
        position_mode_outcome = await refresh_position_mode(
            _position_mode_cache, client, user.id,
            ttl_seconds=settings.exec_position_mode_ttl_seconds,
        )
        dual_side_position = position_mode_outcome.dual_side_position

    market = MarketDataService(client, _market_cache)
    service = ExecutionService(session=session, settings=settings, client=client, market=market)
    try:
        return await service.evaluate(
            user=user,
            signal=signal,
            plan=plan,
            has_trading_key=has_trading_key,
            key_can_trade_futures=key_can_trade_futures,
            permissions_trustworthy=permissions_trustworthy,
            dual_side_position=dual_side_position,
            selected_exchange_mode=selected_mode,
            planned_price=planned_price,
        )
    except ExchangeError as exc:
        logger.warning("Биржа недоступна при оценке исполнения", extra={"user_id": user.id})
        await _record_exchange_error(
            session, user, signal, exc, at_confirm=planned_price is not None
        )
        return _describe(exc)
    finally:
        await client.close()


async def _send_result(
    bot: Bot,
    db: Database,
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
        # Раздел 12а ТЗ: наблюдение за этим отказом уже записано внутри
        # ExecutionService.evaluate() (status REFUSED) — здесь только текст.
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
        quote=result,
        drift_percent=price_drift_percent(result.order.entry_price, signal),
    )
    task = asyncio.create_task(
        _expire_card(
            bot, db, user.id, signal.id, sent.chat.id, sent.message_id,
            settings.exec_confirm_ttl_seconds,
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _expire_card(
    bot: Bot,
    db: Database,
    user_id: int,
    signal_id: int,
    chat_id: int,
    message_id: int,
    ttl_seconds: int,
) -> None:
    """Раздел 5 ТЗ: по истечении TTL кнопки заменяются на «просрочено».

    Сверка по message_id перед правкой: если карточку уже обработали
    (Да/Нет) или заменили пересчитанной после PRICE_DRIFT, задача не
    трогает чужое сообщение — просто выходит.

    Раздел 12а ТЗ: карточка, дожившая до этой точки, точно не была ни
    подтверждена, ни отклонена — пишем EXPIRED в собственной сессии (эта
    задача не участвует в транзакции обработчика, который её запустил).
    """
    await asyncio.sleep(ttl_seconds)
    key = (user_id, signal_id)
    state = _confirmations.get(key)
    if state is None or state.chat_id != chat_id or state.message_id != message_id:
        return
    del _confirmations[key]
    row = build_observation_order_from_quote(
        state.quote.order, status=OrderStatus.EXPIRED, price_drift_percent=state.drift_percent
    )
    async with db.session() as session:
        session.add(row)
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
    db: Database,
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
        session, user, signal, plan, settings, cipher,
        planned_price=None, check_permissions=True,
    )
    await _send_result(
        callback.bot, db, callback.message.chat.id, user, signal, result, settings
    )


@router.callback_query(F.data.startswith(ExecutionCB.NO))
async def confirm_no(callback: CallbackQuery, user: User, session: AsyncSession) -> None:
    """Раздел 12а ТЗ: отказ пользователя пишется тут же, в транзакции этого
    апдейта — карточка ещё жива в _confirmations, повторный поход на биржу
    ради чисел не нужен, они уже посчитаны при показе (state.quote)."""
    signal_id = _parse_signal_id(str(callback.data), ExecutionCB.NO)
    await callback.answer()
    if signal_id is None or not isinstance(callback.message, Message):
        return
    state = _confirmations.pop((user.id, signal_id), None)
    if state is not None:
        row = build_observation_order_from_quote(
            state.quote.order, status=OrderStatus.DECLINED, price_drift_percent=state.drift_percent
        )
        session.add(row)
        await session.flush()
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
    db: Database,
) -> None:
    signal_id = _parse_signal_id(str(callback.data), ExecutionCB.YES)
    if signal_id is None:
        await callback.answer()
        return

    # Раздел 8 ТЗ: защита от двойного нажатия. Занятый ключ — мгновенный
    # ответ "уже обрабатывается", без ожидания и без повторной попытки.
    try:
        ttl = settings.confirm_lock_ttl_seconds
        async with RedisLock(redis, confirm_lock_key(user.id, signal_id), ttl_seconds=ttl):
            await _process_confirm(callback, session, user, signal_id, settings, cipher, db)
    except LockBusyError:
        await callback.answer("Уже обрабатывается…", show_alert=True)


async def _process_confirm(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    signal_id: int,
    settings: Settings,
    cipher: SecretCipher,
    db: Database,
) -> None:
    await callback.answer()
    if not isinstance(callback.message, Message) or callback.bot is None:
        return

    key = (user.id, signal_id)
    state = _confirmations.get(key)
    now = datetime.now(UTC)
    ttl = timedelta(seconds=settings.exec_confirm_ttl_seconds)

    if state is None or state.message_id != callback.message.message_id:
        # Устаревшая карточка (заменена пересчётом после PRICE_DRIFT) или
        # бот перезапускался — нечего логировать как отдельный исход,
        # актуальная карточка (если есть) сама допишет свой результат.
        _confirmations.pop(key, None)
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_reply_markup(reply_markup=expired_keyboard())
        return

    if now - state.created_at > ttl:
        # Раздел 12а ТЗ: "Да" пришло позже TTL — фоновая _expire_card ещё
        # не успела дописать EXPIRED (или уже успела и это гонка), но с
        # точки зрения пользователя карточка истекла; тихий вход недопустим.
        _confirmations.pop(key, None)
        row = build_observation_order_from_quote(
            state.quote.order, status=OrderStatus.EXPIRED, price_drift_percent=state.drift_percent
        )
        session.add(row)
        await session.flush()
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
        session, user, signal, plan, settings, cipher,
        planned_price=state.planned_price, check_permissions=False,
        known_dual_side_position=state.quote.dual_side_position,
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
                session, user, signal, plan, settings, cipher,
                planned_price=None, check_permissions=False,
                known_dual_side_position=state.quote.dual_side_position,
            )
            await callback.message.answer(
                "↻ Цена ушла дальше допустимого. Пересчитал карточку:"
            )
            await _send_result(
                callback.bot, db, callback.message.chat.id, user, signal, new_result, settings
            )
        else:
            await callback.message.edit_text(render_refusal(result), reply_markup=None)
        return

    # Успех: раздел 16 ТЗ, шаг 15.5.1 — состояние сервиса, не зашитый
    # литерал (было: OrderStatus.DRY_RUN безусловно). exec_dry_run=False
    # сегодня недостижим (Settings._dry_run_supported_only_when_true роняет
    # процесс на старте раньше) — ветка ниже на случай обхода валидатора,
    # не тихое повторение DRY_RUN. Путь реальной отправки — шаг 15.5.2.
    if not settings.exec_dry_run:
        raise NotImplementedError(
            "EXEC_DRY_RUN=false не поддерживается до шага 15.5.2"
        )
    status = OrderStatus.DRY_RUN

    _confirmations.pop(key, None)
    order = result.order
    # Дрейф — от свежей цены подтверждения (order.entry_price), не от цены
    # первого показа карточки (state.drift_percent): раздел 5 ТЗ запрашивает
    # цену заново именно на "Да", это и есть цифры на момент входа.
    drift = price_drift_percent(order.entry_price, signal)

    orders_repo = ExecutionOrderRepository(session)
    try:
        # SAVEPOINT: та же техника, что и в _record_exchange_error — если
        # ловим гонку, откатываем только эту вставку, не всю сессию.
        async with session.begin_nested():
            for row in build_execution_orders(order, status, price_drift_percent=drift):
                orders_repo.add(row)
            await orders_repo.flush()
    except IntegrityError as exc:
        if not _is_client_order_id_collision(exc):
            raise
        # Гонка: два параллельных _process_confirm по одному сигналу
        # (двойной тап после протухшего лока, раздел 8 ТЗ) — второй
        # проигрывает на UNIQUE client_order_id. Не помечаем signal.trade_
        # opened_at: это уже сделал победитель.
        logger.info(
            "Гонка на подтверждении: client_order_id уже занят",
            extra={"user_id": user.id, "signal_id": signal.id},
        )
        await callback.message.edit_text(
            "⏳ Сигнал уже обрабатывается — похоже, вход уже отправлен "
            "другим запросом.",
            reply_markup=None,
        )
        return

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
