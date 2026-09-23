"""Read-back входа и спасение стопа (шаг 15.5.3, раздел 8-9 ТЗ).

Ответ биржи на отправку (SUBMITTED) не доказывает, что позиция открыта и
стоп стоит. verify_entry() проверяет это чтением с биржи:

1. исполнение входа — get_order_fill по clientOrderID, строгий разбор
   (нет поля → ReadbackIncomplete, не ноль);
2. UNKNOWN — один поиск по clientOrderID; не нашёлся — остаётся UNKNOWN,
   повторной отправки нет;
3. стоп и тейк — среди openOrders ищутся НАШИ условные ордера
   (find_our_conditional); найденные записываются строками STOP_LOSS /
   TAKE_PROFIT с их orderId;
4. стопа нет — одна попытка отдельного STOP_MARKET с closePosition, по той
   же дисциплине, что и вход: строка PENDING → commit → HTTP → commit;
   не вышло — тревога «ПОЗИЦИЯ БЕЗ СТОПА». Тейка нет — одна попытка, без
   тревоги.

Функция идемпотентна: всё её состояние — в строках execution_orders.
Повторный вызов (reconciler 15.6) не читает заново то, что уже записано,
и не отправляет повторно строки в PENDING/UNKNOWN.

Строки найденных условников получают client_order_id tj{nid}u{uid}S/T —
это НАША метка идемпотентности (UNIQUE: не больше одного стопа и одного
тейка на вход), а не clientOrderId на бирже: у стопа, вложенного во вход,
он может быть пустым. Сверять с биржей такие строки — по exchange_order_id
(orderId); спасённые — по clientOrderID, он у них наш.

Форма ответов get_order/openOrders для исполненного маркет-входа живьём не
снята (раздел 16, 15.5.5): фикстуры тестов — синтетика.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.database.models.execution_order import ExecutionOrder
from app.database.repositories.execution_order import ExecutionOrderRepository
from app.exchanges.base import (
    ExchangeClient,
    ExchangeError,
    OpenOrder,
    OrderFill,
    ReadbackIncomplete,
)
from app.execution.models import OrderRequest
from app.trading.enums import OrderRole, OrderSide, OrderStatus, OrderType

logger = get_logger(__name__)

# Условник должен быть создан не раньше входа: ручной стоп, поставленный до
# нажатия «Да», своим не считается. Запас — на расхождение часов биржи и БД.
PLACED_AFTER_SKEW = timedelta(seconds=5)

# Код ошибки строки-наблюдения «стоп не подтверждён» (openOrders не прочитан).
STOP_UNVERIFIED = "STOP_UNVERIFIED"

Sleep = Callable[[float], Awaitable[None]]


class ConditionalOutcome(StrEnum):
    FOUND = "found"                  # наш условник уже стоит на бирже
    RESCUED = "rescued"              # не было — выставлен отдельно
    RESCUE_FAILED = "rescue_failed"  # не было, выставить не вышло
    AMBIGUOUS = "ambiguous"          # подходят несколько — не присваиваем
    UNVERIFIED = "unverified"        # openOrders не прочитан — неизвестно
    PENDING = "pending"              # строка спасения уже есть, исход неизвестен


@dataclass(frozen=True, slots=True)
class ConditionalState:
    outcome: ConditionalOutcome
    order_id: str | None = None
    trigger_price: Decimal | None = None


@dataclass(slots=True)
class ReadbackResult:
    entry_status: OrderStatus
    fill: OrderFill | None = None
    stop: ConditionalState | None = None
    take: ConditionalState | None = None
    warnings: list[str] = field(default_factory=list)
    alarm: str | None = None

    @property
    def checked(self) -> bool:
        """Дошло ли до проверки стопа/тейка (у REJECTED и не найденного
        UNKNOWN — нет)."""
        return self.stop is not None


def _num(value: Decimal) -> str:
    """Число для текста пользователю — без хвостовых нулей и экспоненты."""
    return format(value.normalize(), "f")


def _quantize(value: Decimal, price_precision: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-price_precision), rounding=ROUND_HALF_UP)


def _closing_side(entry_side: OrderSide) -> OrderSide:
    return OrderSide.SELL if entry_side is OrderSide.BUY else OrderSide.BUY


@dataclass(frozen=True, slots=True)
class Match:
    order: OpenOrder | None
    ambiguous: bool = False


def find_our_conditional(
    open_orders: Iterable[OpenOrder],
    *,
    symbol: str,
    order_type: OrderType,
    position_side: str,
    closing_side: OrderSide,
    level: Decimal,
    price_precision: int,
    placed_after: datetime,
    claimed_order_ids: set[str],
    own_client_order_id: str,
) -> Match:
    """Наш условный ордер среди openOrders — по всем признакам сразу:

    1. тот же символ; 2. тип STOP_MARKET/TAKE_PROFIT_MARKET; 3. наша сторона
    позиции (positionSide); 4. закрывающая сторона (LONG → SELL); 5. stopPrice
    верхнего уровня не 0 и совпадает с уровнем, ушедшим в запрос, до шага
    цены символа; 6. создан не раньше входа (минус PLACED_AFTER_SKEW) —
    ручной стоп, поставленный до «Да», отсекается; 7. orderId не записан за
    другим входом; 8. clientOrderId с префиксом tj — только наш
    (tj{nid}u{uid}…): чужой tj — другой вход бота.

    Ровно один кандидат — наш. Ноль — нет. Больше одного — ambiguous: не
    присваиваем и не спасаем (остаточный риск — ручной условник того же
    типа и стороны на той же цене с точностью до тика, поставленный в
    секунды после входа)."""
    target = _quantize(level, price_precision)
    own_prefix = own_client_order_id[:-1]  # tj{nid}u{uid} без буквы роли
    candidates = []
    for o in open_orders:
        if o.symbol != symbol or o.order_type != order_type.value:
            continue
        if o.position_side != position_side or o.side != closing_side.value:
            continue
        if o.stop_price == 0 or _quantize(o.stop_price, price_precision) != target:
            continue
        if o.created_at < placed_after - PLACED_AFTER_SKEW:
            continue
        if o.order_id in claimed_order_ids:
            continue
        if o.client_order_id.startswith("tj") and not o.client_order_id.startswith(own_prefix):
            continue
        candidates.append(o)
    if len(candidates) == 1:
        return Match(candidates[0])
    return Match(None, ambiguous=len(candidates) > 1)


def _entry_status_from_fill(fill: OrderFill) -> OrderStatus:
    return OrderStatus.FILLED if fill.status == "FILLED" else OrderStatus.SUBMITTED


async def verify_entry(
    *,
    session: AsyncSession,
    client: ExchangeClient,
    settings: Settings,
    entry_row: ExecutionOrder,
    order: OrderRequest,
    position_side: str,
    price_precision: int,
    sleep: Sleep = asyncio.sleep,
) -> ReadbackResult:
    """См. докстринг модуля. Каждый HTTP — max_retries=1: путь «Да» держит
    Redis-лок, TTL которого посчитан на один таймаут на вызов."""
    result = ReadbackResult(entry_status=entry_row.status)
    if entry_row.status not in (OrderStatus.SUBMITTED, OrderStatus.UNKNOWN, OrderStatus.FILLED):
        return result

    fill = await _read_fill(session, client, settings, entry_row, order, result, sleep)
    result.fill = fill
    result.entry_status = entry_row.status
    if entry_row.status is OrderStatus.UNKNOWN:
        # Поиск не нашёл ордер (или биржа не ответила) — повторной отправки
        # нет, стоп не проверяем: позиции может и не быть.
        return result

    await _check_conditionals(
        session, client, settings, entry_row, order, position_side, price_precision,
        fill, result, sleep,
    )
    return result


async def _read_fill(
    session: AsyncSession,
    client: ExchangeClient,
    settings: Settings,
    entry_row: ExecutionOrder,
    order: OrderRequest,
    result: ReadbackResult,
    sleep: Sleep,
) -> OrderFill | None:
    """Исполнение входа. UNKNOWN: один поиск после паузы; нашёлся — дальше
    как SUBMITTED. SUBMITTED: до exec_order_readback_attempts чтений, пока
    биржа не скажет FILLED. Статус строки и raw_response обновляются и
    коммитятся здесь же."""
    symbol = order.symbol
    cid = order.entry_client_order_id
    fill: OrderFill | None = None

    if entry_row.status is OrderStatus.UNKNOWN:
        await sleep(settings.exec_unknown_search_delay_ms / 1000)
        try:
            fill = await client.get_order_fill(symbol, cid, max_retries=1)
        except ReadbackIncomplete as exc:
            # Ордер нашёлся (биржа отдала объект), но без обязательного
            # поля — позиция могла открыться: дальше как SUBMITTED.
            _incomplete(result, exc, entry_row)
            entry_row.status = OrderStatus.SUBMITTED
            await session.commit()
            return None
        except ExchangeError as exc:
            logger.warning(
                "Поиск входа в статусе UNKNOWN: не нашёлся или биржа не ответила",
                extra={"client_order_id": cid, "error": str(exc), "code": exc.code},
            )
            result.warnings.append(
                "⚠️ Биржа не подтвердила ордер — проверь позиции в BingX. "
                "Повторно не отправляю."
            )
            return None
        entry_row.status = OrderStatus.SUBMITTED
        entry_row.exchange_order_id = entry_row.exchange_order_id or fill.order_id or None
        await session.commit()
        if fill.status == "FILLED":
            _apply_fill(entry_row, fill, result)
            await session.commit()
            return fill

    if entry_row.status is OrderStatus.FILLED and fill is None:
        # Повторный вызов (reconciler): исполнение уже прочитано и
        # записано — не читаем заново.
        return None

    last_error: ExchangeError | None = None
    for attempt in range(settings.exec_order_readback_attempts):
        if attempt:
            await sleep(settings.exec_order_readback_delay_ms / 1000)
        try:
            fill = await client.get_order_fill(symbol, cid, max_retries=1)
        except ReadbackIncomplete as exc:
            _incomplete(result, exc, entry_row)
            await session.commit()
            return None
        except ExchangeError as exc:
            last_error = exc
            continue
        if fill.status == "FILLED":
            break

    if fill is None:
        logger.warning(
            "Не удалось прочитать исполнение входа",
            extra={"client_order_id": cid, "error": str(last_error)},
        )
        result.warnings.append(
            "⚠️ Не смог прочитать исполнение — проверь позицию в BingX."
        )
        return None

    _apply_fill(entry_row, fill, result)
    await session.commit()
    return fill


def _incomplete(result: ReadbackResult, exc: ReadbackIncomplete, entry_row: ExecutionOrder) -> None:
    logger.error(
        "Read-back: в ответе чтения ордера нет обязательного поля",
        extra={"field": exc.field, "client_order_id": entry_row.client_order_id},
    )
    result.warnings.append(
        f"⚠️ Не смог прочитать исполнение (нет поля {exc.field}) — проверь позицию в BingX."
    )
    entry_row.raw_response = {"submit": entry_row.raw_response, "readback": exc.payload}


def _apply_fill(entry_row: ExecutionOrder, fill: OrderFill, result: ReadbackResult) -> None:
    entry_row.status = _entry_status_from_fill(fill)
    entry_row.exchange_order_id = entry_row.exchange_order_id or fill.order_id or None
    submit_raw = entry_row.raw_response
    if isinstance(submit_raw, dict) and "readback" in submit_raw:
        submit_raw = submit_raw.get("submit")
    entry_row.raw_response = {"submit": submit_raw, "readback": fill.raw}
    if fill.status != "FILLED" or fill.executed_qty < fill.orig_qty:
        logger.warning(
            "Вход исполнен не полностью",
            extra={
                "client_order_id": entry_row.client_order_id,
                "status": fill.status,
                "executed_qty": str(fill.executed_qty),
                "orig_qty": str(fill.orig_qty),
            },
        )
        result.warnings.append(
            f"⚠️ Исполнено {_num(fill.executed_qty)} из {_num(fill.orig_qty)} "
            f"(статус {fill.status}). Стоп всё равно проверяю."
        )


async def _check_conditionals(
    session: AsyncSession,
    client: ExchangeClient,
    settings: Settings,
    entry_row: ExecutionOrder,
    order: OrderRequest,
    position_side: str,
    price_precision: int,
    fill: OrderFill | None,
    result: ReadbackResult,
    sleep: Sleep,
) -> None:
    repo = ExecutionOrderRepository(session)
    stop_row = await repo.get_by_client_order_id(order.user_id, order.stop_loss_client_order_id)
    take_row = await repo.get_by_client_order_id(order.user_id, order.take_profit_client_order_id)
    if stop_row is not None:
        result.stop = _state_from_row(stop_row)
    if take_row is not None:
        result.take = _state_from_row(take_row)
    if stop_row is not None and take_row is not None:
        _raise_alarm_if_unprotected(result, order, fill)
        return

    claimed = await repo.claimed_conditional_order_ids(
        order.user_id, exclude_notification_id=order.notification_id
    )
    closing = _closing_side(order.side)

    def match(orders: list[OpenOrder], order_type: OrderType, level: Decimal, cid: str) -> Match:
        return find_our_conditional(
            orders,
            symbol=order.symbol,
            order_type=order_type,
            position_side=position_side,
            closing_side=closing,
            level=level,
            price_precision=price_precision,
            placed_after=entry_row.created_at,
            claimed_order_ids=claimed,
            own_client_order_id=cid,
        )

    try:
        orders = await client.get_open_orders(order.symbol, max_retries=1)
    except ExchangeError as exc:
        await _mark_unverified(session, order, result, fill, stop_row, take_row, str(exc))
        return

    stop_match = match(
        orders, OrderType.STOP_MARKET, order.stop_loss, order.stop_loss_client_order_id
    )
    take_match = match(
        orders, OrderType.TAKE_PROFIT_MARKET, order.take_profit, order.take_profit_client_order_id
    )
    missing_stop = stop_row is None and stop_match.order is None and not stop_match.ambiguous
    missing_take = take_row is None and take_match.order is None and not take_match.ambiguous
    if missing_stop or missing_take:
        # Р2: вложенный условник может стать отдельным ордером не сразу —
        # «нет» только после второго чтения.
        await sleep(settings.exec_open_orders_recheck_delay_ms / 1000)
        try:
            orders = await client.get_open_orders(order.symbol, max_retries=1)
        except ExchangeError as exc:
            await _mark_unverified(session, order, result, fill, stop_row, take_row, str(exc))
            return
        stop_match = match(
            orders, OrderType.STOP_MARKET, order.stop_loss, order.stop_loss_client_order_id
        )
        take_match = match(
            orders, OrderType.TAKE_PROFIT_MARKET, order.take_profit,
            order.take_profit_client_order_id,
        )

    if stop_row is None:
        result.stop = await _resolve(
            session, client, order, position_side, OrderRole.STOP_LOSS, stop_match, result
        )
    if take_row is None:
        result.take = await _resolve(
            session, client, order, position_side, OrderRole.TAKE_PROFIT, take_match, result
        )
    _raise_alarm_if_unprotected(result, order, fill)


def _state_from_row(row: ExecutionOrder) -> ConditionalState:
    if row.status is OrderStatus.SUBMITTED:
        outcome = (
            ConditionalOutcome.FOUND
            if isinstance(row.raw_response, dict) and row.raw_response.get("found")
            else ConditionalOutcome.RESCUED
        )
        return ConditionalState(outcome, row.exchange_order_id, row.trigger_price)
    if row.status in (OrderStatus.PENDING, OrderStatus.UNKNOWN):
        return ConditionalState(
            ConditionalOutcome.PENDING, row.exchange_order_id, row.trigger_price
        )
    return ConditionalState(ConditionalOutcome.RESCUE_FAILED, None, row.trigger_price)


def _conditional_row(
    order: OrderRequest, role: OrderRole, *, status: OrderStatus, level: Decimal
) -> ExecutionOrder:
    is_stop = role is OrderRole.STOP_LOSS
    return ExecutionOrder(
        user_id=order.user_id,
        signal_id=order.signal_id,
        notification_id=order.notification_id,
        client_order_id=(
            order.stop_loss_client_order_id if is_stop else order.take_profit_client_order_id
        ),
        symbol=order.symbol,
        side=_closing_side(order.side),
        position_side=order.position_side,
        order_type=OrderType.STOP_MARKET if is_stop else OrderType.TAKE_PROFIT_MARKET,
        role=role,
        # closePosition: объём не задаётся — закрывается вся позиция.
        quantity=None,
        trigger_price=level,
        status=status,
    )


async def _resolve(
    session: AsyncSession,
    client: ExchangeClient,
    order: OrderRequest,
    position_side: str,
    role: OrderRole,
    found: Match,
    result: ReadbackResult,
) -> ConditionalState:
    is_stop = role is OrderRole.STOP_LOSS
    level = order.stop_loss if is_stop else order.take_profit
    label = "стоп" if is_stop else "тейк"

    if found.ambiguous:
        result.warnings.append(
            f"⚠️ Не могу отличить свой {label} от выставленного руками по "
            f"{order.symbol} — проверь в BingX."
        )
        return ConditionalState(ConditionalOutcome.AMBIGUOUS, None, level)

    if found.order is not None:
        o = found.order
        row = _conditional_row(order, role, status=OrderStatus.SUBMITTED, level=o.stop_price)
        row.exchange_order_id = o.order_id or None
        row.raw_response = {
            "found": True,
            "orderId": o.order_id,
            "clientOrderId": o.client_order_id,
            "stopPrice": str(o.stop_price),
            "type": o.order_type,
        }
        if not await _insert(session, row):
            return _state_from_row(await _existing(session, order, role))
        await session.commit()
        return ConditionalState(ConditionalOutcome.FOUND, o.order_id, o.stop_price)

    # Не нашёлся — одна попытка выставить отдельно. PENDING до HTTP.
    row = _conditional_row(order, role, status=OrderStatus.PENDING, level=level)
    if not await _insert(session, row):
        return _state_from_row(await _existing(session, order, role))
    await session.commit()

    try:
        placed = await client.place_conditional_order(
            symbol=order.symbol,
            side=_closing_side(order.side),
            position_side=position_side,
            order_type=(OrderType.STOP_MARKET if is_stop else OrderType.TAKE_PROFIT_MARKET).value,
            stop_price=level,
            client_order_id=row.client_order_id or "",
        )
    except ExchangeError as exc:
        row.status = (
            OrderStatus.REJECTED if exc.code is not None and exc.code != 0 else OrderStatus.UNKNOWN
        )
        row.error_code = str(exc.code) if row.status is OrderStatus.REJECTED else type(exc).__name__
        row.raw_response = exc.payload
        await session.commit()
        return ConditionalState(ConditionalOutcome.RESCUE_FAILED, None, level)
    except Exception as exc:
        # Как в submit_entry_order: после ушедшего запроса любой
        # непредвиденный сбой — «исход неизвестен», трейс в лог.
        logger.exception(
            "Непредвиденный сбой после отправки условного ордера — исход неизвестен",
            extra={"client_order_id": row.client_order_id},
        )
        row.status = OrderStatus.UNKNOWN
        row.error_code = type(exc).__name__
        await session.commit()
        return ConditionalState(ConditionalOutcome.RESCUE_FAILED, None, level)

    row.status = OrderStatus.SUBMITTED
    row.exchange_order_id = placed.order_id or None
    row.raw_response = placed.raw
    await session.commit()
    return ConditionalState(ConditionalOutcome.RESCUED, placed.order_id or None, level)


async def _insert(session: AsyncSession, row: ExecutionOrder) -> bool:
    """False — строку с этим client_order_id уже вставил параллельный
    verify_entry (UNIQUE): не дублируем и не отправляем повторно."""
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        return False
    return True


async def _existing(session: AsyncSession, order: OrderRequest, role: OrderRole) -> ExecutionOrder:
    cid = (
        order.stop_loss_client_order_id
        if role is OrderRole.STOP_LOSS
        else order.take_profit_client_order_id
    )
    row = await ExecutionOrderRepository(session).get_by_client_order_id(order.user_id, cid)
    assert row is not None, "UNIQUE сработал, а строки нет"
    return row


async def _mark_unverified(
    session: AsyncSession,
    order: OrderRequest,
    result: ReadbackResult,
    fill: OrderFill | None,
    stop_row: ExecutionOrder | None,
    take_row: ExecutionOrder | None,
    error: str,
) -> None:
    """Р3: openOrders не прочитан — есть ли стоп, неизвестно. Вслепую не
    спасаем (второй стоп). Тревога и строка-наблюдение без client_order_id
    (это не ордер: повторный verify_entry проверит заново), по которой
    сводка покажет аномалию."""
    logger.error(
        "Read-back: не удалось прочитать openOrders — стоп не подтверждён",
        extra={"symbol": order.symbol, "notification_id": order.notification_id, "error": error},
    )
    if stop_row is None:
        row = _conditional_row(
            order, OrderRole.STOP_LOSS, status=OrderStatus.ERROR, level=order.stop_loss
        )
        row.client_order_id = None
        row.error_code = STOP_UNVERIFIED
        session.add(row)
        await session.commit()
        result.stop = ConditionalState(ConditionalOutcome.UNVERIFIED, None, order.stop_loss)
        qty = fill.executed_qty if fill is not None else order.quantity
        result.alarm = (
            f"⚠️ СТОП НЕ ПОДТВЕРЖДЁН: {order.symbol} {order.position_side.value} "
            f"{_num(qty)} — проверь позицию в BingX."
        )
    if take_row is None:
        result.take = ConditionalState(ConditionalOutcome.UNVERIFIED, None, order.take_profit)


def _raise_alarm_if_unprotected(
    result: ReadbackResult, order: OrderRequest, fill: OrderFill | None
) -> None:
    if result.stop is not None and result.stop.outcome is ConditionalOutcome.RESCUE_FAILED:
        qty = fill.executed_qty if fill is not None else order.quantity
        result.alarm = (
            f"⚠️ ПОЗИЦИЯ БЕЗ СТОПА: {order.symbol} {order.position_side.value} "
            f"{_num(qty)} — поставь стоп руками."
        )
        logger.error(
            "Позиция без стопа: спасение стопа не удалось",
            extra={
                "symbol": order.symbol,
                "notification_id": order.notification_id,
                "client_order_id": order.stop_loss_client_order_id,
            },
        )
    if result.take is not None and result.take.outcome is ConditionalOutcome.RESCUE_FAILED:
        result.warnings.append("Тейк не выставился — поставь руками, если нужен.")
