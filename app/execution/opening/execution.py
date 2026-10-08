"""Исполнение открытия: вход, read-back, защита, журнал (§5 шаги 7–11 плана).

Общий для «Открыть» (service.confirm) и восстановления после рестарта
(recovery): каждый шаг читает состояние из БД и биржи и ничего не делает
дважды. Новый вход отправляется только из confirm и только один раз —
строка ENTRY в execution_orders (UNIQUE client_order_id) коммитится ДО HTTP.

Защита позиции (разведка Р3/Р4/Р6, 05.10):
1. вложенный стоп ищется в openOrders по positionID, типу, стороне закрытия и
   цене (clientOrderId у него пустой);
2. нет или объём меньше позиции — запасной STOP_MARKET closePosition + quantity
   = объём позиции (правило разведки 02.10);
3. запасной не встал — аварийное закрытие маркетом, сделка в журнал с выходом;
4. закрытие не прошло — ALARM: тревога и повтор циклом восстановления.
Ликвидация по факту между входом и стопом — тоже аварийное закрытие.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.numfmt import fmt_price, fmt_qty
from app.database.models.execution_order import ExecutionOrder
from app.database.models.trade_opening import TradeOpening
from app.database.repositories.trade import TradeRepository
from app.exchanges.base import (
    ExchangeClient,
    ExchangeError,
    ExchangeUnavailableError,
    OpenOrder,
    OrderFill,
    Position,
    TpSlSpec,
)
from app.trading.calculations import CalculationError
from app.trading.enums import (
    EntryType,
    OpeningStatus,
    OrderRole,
    OrderSide,
    OrderStatus,
    OrderType,
    TradeSide,
    TradeSource,
)
from app.trading.journal import JournalError, TradeJournal

logger = get_logger(__name__)

ZERO = Decimal(0)
ORDER_NOT_EXIST_CODE = 109421
OPENING_SIDE = {TradeSide.LONG: OrderSide.BUY, TradeSide.SHORT: OrderSide.SELL}
CLOSING_SIDE = {TradeSide.LONG: OrderSide.SELL, TradeSide.SHORT: OrderSide.BUY}
EXIT_REASON_EMERGENCY = "Аварийное закрытие: стоп не встал"
EXIT_REASON_LIQUIDATION = "Аварийное закрытие: ликвидация ближе стопа"
PROTECT_ATTEMPTS = 3

Sleep = Callable[[float], Awaitable[None]]


def opening_client_order_id(opening_id: int, user_id: int, letter: str, n: int = 0) -> str:
    """to{opening}u{user}{e|s|t|c}[n] — нижний регистр (BingX хранит
    clientOrderId строчными), ≤ 40 символов. n — номер повтора запасного
    стопа/закрытия (у входа всегда 0: вход отправляется ровно один раз)."""
    return f"to{opening_id}u{user_id}{letter}{n if n else ''}"


@dataclass(frozen=True, slots=True)
class EntryResult:
    status: OpeningStatus          # SUBMITTING→FILLED/WORKING/REJECTED/UNKNOWN
    message: str = ""


@dataclass(frozen=True, slots=True)
class ProtectResult:
    status: OpeningStatus          # PROTECTED / EMERGENCY_CLOSED / ALARM
    take_missing: bool = False
    fallback_stop: bool = False
    warnings: tuple[str, ...] = ()
    close_fill: OrderFill | None = None
    reason: str = ""


async def transition(
    session: AsyncSession,
    opening: TradeOpening,
    from_: tuple[OpeningStatus, ...],
    to: OpeningStatus,
    **values: Any,
) -> bool:
    """Условный переход статуса: rowcount ≠ 1 — открытие уже взял другой
    обработчик (двойное нажатие, Mini App и чат, восстановление)."""
    if to in (
        OpeningStatus.DONE, OpeningStatus.REJECTED, OpeningStatus.NOT_PLACED,
        OpeningStatus.CANCELLED, OpeningStatus.EXPIRED, OpeningStatus.EMERGENCY_CLOSED,
        OpeningStatus.REFUSED, OpeningStatus.DRY_RUN, OpeningStatus.DECLINED,
        OpeningStatus.EXPIRED_CARD,
    ):
        values.setdefault("finished_at", datetime.now(UTC))
    result = await session.execute(
        update(TradeOpening)
        .where(TradeOpening.id == opening.id, TradeOpening.status.in_(from_))
        .values(status=to, **values)
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        return False
    await session.commit()
    await session.refresh(opening)
    return True


class Runner:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        client: ExchangeClient,
        opening: TradeOpening,
        *,
        price_precision: int,
        quantity_precision: int,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.session = session
        self.settings = settings
        self.client = client
        self.opening = opening
        self.pp = price_precision
        self.qp = quantity_precision
        self._sleep = sleep

    @property
    def side(self) -> TradeSide:
        return self.opening.side

    def cid(self, letter: str, n: int = 0) -> str:
        return opening_client_order_id(self.opening.id, self.opening.user_id, letter, n)

    def _row(
        self, role: OrderRole, order_type: OrderType, side: OrderSide, **kw: Any
    ) -> ExecutionOrder:
        return ExecutionOrder(
            user_id=self.opening.user_id, trade_opening_id=self.opening.id,
            symbol=self.opening.symbol, side=side, position_side=self.side,
            order_type=order_type, role=role, **kw,
        )

    async def _rows(self, role: OrderRole) -> list[ExecutionOrder]:
        return list(await self.session.scalars(
            select(ExecutionOrder).where(
                ExecutionOrder.trade_opening_id == self.opening.id, ExecutionOrder.role == role
            ).order_by(ExecutionOrder.id)
        ))

    async def entry_row(self) -> ExecutionOrder | None:
        rows = await self._rows(OrderRole.ENTRY)
        return rows[0] if rows else None

    # --- вход ------------------------------------------------------------------

    async def place_entry(self, quantity: Decimal) -> EntryResult:
        """CONFIRMED → SUBMITTING → POST. Один раз: строка ENTRY с cid
        коммитится до HTTP; повтор с тем же cid упрётся в UNIQUE."""
        o = self.opening
        cid = self.cid("e")
        is_limit = o.entry_type is EntryType.LIMIT
        row = self._row(
            OrderRole.ENTRY, OrderType.LIMIT if is_limit else OrderType.MARKET,
            OPENING_SIDE[self.side], client_order_id=cid, quantity=quantity,
            price=o.limit_price if is_limit else None, card_price=o.card_price,
            card_quantity=o.quantity, trigger_price=None, leverage=o.leverage,
            status=OrderStatus.PENDING, stage="confirm",
        )
        self.session.add(row)
        try:
            await self.session.flush()
        except IntegrityError:
            await self.session.rollback()
            return EntryResult(OpeningStatus.UNKNOWN, "вход по этому открытию уже отправлялся")
        if not await transition(
            self.session, o, (OpeningStatus.CONFIRMED,), OpeningStatus.SUBMITTING,
            quantity=quantity,
        ):
            return EntryResult(OpeningStatus.UNKNOWN, "открытие уже обрабатывается")
        stop = TpSlSpec(trigger_price=o.stop_loss)
        take = TpSlSpec(trigger_price=o.take_profit) if o.take_profit is not None else None
        common: dict[str, Any] = dict(
            symbol=o.symbol, side=OPENING_SIDE[self.side], position_side=self.side.value,
            quantity=quantity, client_order_id=cid, stop_loss=stop, take_profit=take,
        )
        try:
            if is_limit:
                assert o.limit_price is not None
                result = await self.client.place_limit_order(price=o.limit_price, **common)
            else:
                result = await self.client.place_market_order(**common)
        except ExchangeError as exc:
            row = await self.entry_row()  # type: ignore[assignment]
            assert row is not None
            if isinstance(exc, ExchangeUnavailableError) or exc.code is None:
                # Ответа нет — ордер мог уйти. Только поиск по cid, без повтора.
                row.status = OrderStatus.UNKNOWN
                row.error_code = type(exc).__name__
                await self.session.commit()
                await transition(
                    self.session, o, (OpeningStatus.SUBMITTING,), OpeningStatus.UNKNOWN,
                    error_code="ENTRY_NO_ANSWER",
                    error_message="Биржа не ответила на вход — проверяю по clientOrderId.",
                )
                logger.warning("Вход открытия без ответа биржи", extra=self._log())
                return EntryResult(OpeningStatus.UNKNOWN)
            row.status = OrderStatus.REJECTED
            row.error_code = str(exc.code)
            row.error_message = str(exc)
            await self.session.commit()
            await transition(
                self.session, o, (OpeningStatus.SUBMITTING,), OpeningStatus.REJECTED,
                error_code=f"EXCHANGE_{exc.code}", error_message=str(exc),
            )
            logger.info("Вход открытия отклонён биржей", extra={**self._log(), "code": exc.code})
            return EntryResult(OpeningStatus.REJECTED, str(exc))
        row = await self.entry_row()  # type: ignore[assignment]
        assert row is not None
        row.exchange_order_id = result.order_id or None
        row.status = OrderStatus.SUBMITTED
        row.raw_response = {"status": result.status}
        o.entry_order_id = result.order_id or None
        await self.session.commit()
        if is_limit and result.status in ("PENDING", "NEW"):
            row.status = OrderStatus.WORKING
            await self.session.commit()
            return EntryResult(OpeningStatus.WORKING)
        return EntryResult(OpeningStatus.FILLED)

    async def read_fill(self, attempts: int | None = None) -> tuple[OrderFill | None, bool]:
        """GET входа по cid. (fill, not_found): not_found — биржа ответила
        109421 на последнюю попытку."""
        tries = attempts or self.settings.exec_order_readback_attempts
        delay = self.settings.exec_order_readback_delay_ms / 1000
        fill: OrderFill | None = None
        not_found = False
        for attempt in range(tries):
            if attempt:
                await self._sleep(delay)
            try:
                fill = await self.client.get_order_fill(
                    self.opening.symbol, self.cid("e"), max_retries=1
                )
                not_found = False
            except ExchangeError as exc:
                not_found = exc.code == ORDER_NOT_EXIST_CODE
                continue
            if fill.status == "FILLED" and fill.executed_qty > ZERO:
                return fill, False
        return fill, not_found

    async def apply_fill(self, fill: OrderFill) -> bool:
        """Исполнение входа → строка ENTRY и открытие FILLED."""
        row = await self.entry_row()
        if row is not None:
            row.status = OrderStatus.FILLED
            row.exchange_order_id = fill.order_id
            await self.session.commit()
        return await transition(
            self.session, self.opening,
            (OpeningStatus.SUBMITTING, OpeningStatus.UNKNOWN, OpeningStatus.WORKING),
            OpeningStatus.FILLED,
            entry_order_id=fill.order_id, filled_qty=fill.executed_qty, avg_price=fill.avg_price,
            entry_fee=fill.fee, filled_at=fill.filled_at or datetime.now(UTC),
            position_id=fill.position_id,
        )

    # --- защита -------------------------------------------------------------------

    def _q(self, price: Decimal) -> Decimal:
        return price.quantize(Decimal(1).scaleb(-self.pp), rounding=ROUND_HALF_UP)

    def _match(
        self, orders: list[OpenOrder], order_type: OrderType, level: Decimal, position: Position
    ) -> OpenOrder | None:
        """Наш стоп/тейк на позиции. Несколько (частичное исполнение лимита —
        вложенный на каждую часть, плюс запасной closePosition): первым —
        closePosition (покрывает весь остаток), иначе с наибольшим объёмом."""
        target = self._q(level)
        candidates = [
            o for o in orders
            if o.symbol == self.opening.symbol and o.order_type == order_type.value
            and o.position_side == self.side.value
            and o.side == CLOSING_SIDE[self.side].value
            and o.stop_price is not None and self._q(o.stop_price) == target
            and (o.position_id is None or position.position_id is None
                 or o.position_id == position.position_id)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda o: (o.close_position, o.quantity))

    async def position(self) -> Position | None:
        positions = await self.client.get_positions(max_retries=1)
        return next(
            (p for p in positions if p.symbol == self.opening.symbol and p.side is self.side),
            None,
        )

    async def _record_conditional(self, role: OrderRole, order: OpenOrder) -> None:
        known = {r.exchange_order_id for r in await self._rows(role)}
        if order.order_id in known:
            return
        order_type = (
            OrderType.STOP_MARKET if role is OrderRole.STOP_LOSS else OrderType.TAKE_PROFIT_MARKET
        )
        self.session.add(self._row(
            role, order_type, CLOSING_SIDE[self.side],
            client_order_id=order.client_order_id or None, exchange_order_id=order.order_id,
            quantity=order.quantity, trigger_price=order.stop_price,
            status=OrderStatus.SUBMITTED, stage="confirm",
        ))
        await self.session.commit()

    async def protect(self) -> ProtectResult:
        """FILLED/ALARM → PROTECTED | EMERGENCY_CLOSED | ALARM."""
        o = self.opening
        position = await self.position()
        if position is None:
            # Позиции уже нет (стоп сработал за секунды, закрыта руками) —
            # защищать нечего; выход запишет reconciler по истории.
            return ProtectResult(OpeningStatus.PROTECTED, warnings=(
                "Позиции на бирже уже нет — выход запишет сверка.",
            ))
        stop: OpenOrder | None = None
        take: OpenOrder | None = None
        orders: list[OpenOrder] = []
        for attempt in range(PROTECT_ATTEMPTS):
            if attempt:
                await self._sleep(self.settings.exec_order_readback_delay_ms / 1000)
            orders = await self.client.get_open_orders(o.symbol, max_retries=1)
            stop = self._match(orders, OrderType.STOP_MARKET, o.stop_loss, position)
            if o.take_profit is not None:
                take = self._match(orders, OrderType.TAKE_PROFIT_MARKET, o.take_profit, position)
            if stop is not None and (o.take_profit is None or take is not None):
                break
        fallback = False
        if stop is not None and (stop.close_position or stop.quantity >= position.quantity):
            await self._record_conditional(OrderRole.STOP_LOSS, stop)
        else:
            if stop is not None:
                await self._record_conditional(OrderRole.STOP_LOSS, stop)
            fallback = True
            placed = await self._fallback_stop(position)
            if not placed:
                return await self._emergency(position, EXIT_REASON_EMERGENCY)
        if take is not None:
            await self._record_conditional(OrderRole.TAKE_PROFIT, take)
        warnings: list[str] = []
        if o.margin_type == "ISOLATED" and position.liquidation_price is not None:
            sign = Decimal(self.side.direction)
            liq = position.liquidation_price
            if (o.stop_loss - liq) * sign <= ZERO:
                return await self._emergency(position, EXIT_REASON_LIQUIDATION)
            entry = position.entry_price
            ratio = (entry - liq) * sign / ((entry - o.stop_loss) * sign or Decimal(1))
            if ratio < self.settings.exec_open_liq_buffer:
                warnings.append(
                    f"Ликвидация {fmt_price(liq, self.pp)} ближе запаса: дальше стопа в "
                    f"{ratio.quantize(Decimal('0.1'))} раза."
                )
        return ProtectResult(
            OpeningStatus.PROTECTED,
            take_missing=o.take_profit is not None and take is None,
            fallback_stop=fallback, warnings=tuple(warnings),
        )

    async def _attempt_no(self, role: OrderRole) -> int:
        count = await self.session.scalar(
            select(func.count()).select_from(ExecutionOrder).where(
                ExecutionOrder.trade_opening_id == self.opening.id,
                ExecutionOrder.role == role,
                ExecutionOrder.client_order_id.is_not(None),
            )
        )
        return int(count or 0) + 1

    async def _fallback_stop(self, position: Position) -> bool:
        o = self.opening
        n = await self._attempt_no(OrderRole.STOP_LOSS)
        cid = self.cid("s", n)
        row = self._row(
            OrderRole.STOP_LOSS, OrderType.STOP_MARKET, CLOSING_SIDE[self.side],
            client_order_id=cid, quantity=position.quantity, trigger_price=o.stop_loss,
            status=OrderStatus.PENDING, stage="confirm",
        )
        self.session.add(row)
        await self.session.commit()
        logger.warning("Вложенный стоп не найден — ставлю запасной", extra=self._log())
        try:
            result = await self.client.place_conditional_order(
                symbol=o.symbol, side=CLOSING_SIDE[self.side], position_side=self.side.value,
                order_type=OrderType.STOP_MARKET.value, stop_price=o.stop_loss,
                quantity=position.quantity, client_order_id=cid, close_position=True,
            )
        except ExchangeError as exc:
            row.status = (
                OrderStatus.UNKNOWN if isinstance(exc, ExchangeUnavailableError)
                else OrderStatus.REJECTED
            )
            row.error_code = str(exc.code) if exc.code is not None else type(exc).__name__
            row.error_message = str(exc)
            await self.session.commit()
            logger.error("Запасной стоп не встал", extra={**self._log(), "error": row.error_code})
            # Без ответа ордер мог встать — проверяем чтением.
            if not isinstance(exc, ExchangeUnavailableError):
                return False
        else:
            row.exchange_order_id = result.order_id or None
        orders = await self.client.get_open_orders(o.symbol, max_retries=1)
        found = next(
            (x for x in orders if x.client_order_id.casefold() == cid.casefold()), None
        ) or self._match(orders, OrderType.STOP_MARKET, o.stop_loss, position)
        if found is None:
            row.status = OrderStatus.REJECTED if row.status is OrderStatus.PENDING else row.status
            await self.session.commit()
            return False
        row.status = OrderStatus.SUBMITTED
        row.exchange_order_id = found.order_id
        await self.session.commit()
        return True

    async def _emergency(self, position: Position, reason: str) -> ProtectResult:
        o = self.opening
        n = await self._attempt_no(OrderRole.CLOSE)
        cid = self.cid("c", n)
        row = self._row(
            OrderRole.CLOSE, OrderType.MARKET, CLOSING_SIDE[self.side], client_order_id=cid,
            quantity=position.quantity, status=OrderStatus.PENDING, stage="confirm",
        )
        self.session.add(row)
        await self.session.commit()
        logger.error("Аварийное закрытие позиции открытия", extra={**self._log(), "reason": reason})
        try:
            await self.client.place_market_order(
                symbol=o.symbol, side=CLOSING_SIDE[self.side], position_side=self.side.value,
                quantity=position.quantity, client_order_id=cid,
            )
        except ExchangeError as exc:
            row.status = (
                OrderStatus.UNKNOWN if isinstance(exc, ExchangeUnavailableError)
                else OrderStatus.REJECTED
            )
            row.error_code = str(exc.code) if exc.code is not None else type(exc).__name__
            await self.session.commit()
        fill: OrderFill | None = None
        try:
            fill = await self.client.get_order_fill(o.symbol, cid, max_retries=1)
        except ExchangeError:
            fill = None
        left = await self.position()
        if fill is not None and fill.status == "FILLED" and left is None:
            row.status = OrderStatus.FILLED
            row.exchange_order_id = fill.order_id
            await self.session.commit()
            return ProtectResult(OpeningStatus.EMERGENCY_CLOSED, close_fill=fill, reason=reason)
        await self.session.commit()
        return ProtectResult(OpeningStatus.ALARM, reason=reason)

    # --- журнал ---------------------------------------------------------------------

    async def record_trade(self, close_fill: OrderFill | None = None, reason: str = "") -> int:
        """Сделка в журнал после подтверждённого исполнения: вход с orderId
        входа (импорт её не заведёт второй раз), счёт, equity карточки,
        positionId. Аварийное закрытие — сразу и выход."""
        o = self.opening
        if o.trade_id is not None:
            return o.trade_id
        assert o.avg_price is not None and o.filled_qty is not None
        journal = TradeJournal(TradeRepository(self.session))
        stop: Decimal | None = o.stop_loss
        if (o.avg_price - o.stop_loss) * Decimal(self.side.direction) <= ZERO:
            stop = None   # проскальзывание за стоп: журнал не примет такой стоп
        try:
            trade = await journal.open_trade(
                user_id=o.user_id, symbol=o.symbol, side=self.side, entry_price=o.avg_price,
                quantity=o.filled_qty, stop_loss=stop, take_profit=o.take_profit,
                leverage=o.leverage, fee=o.entry_fee or ZERO, account_balance=o.equity,
                opened_at=o.filled_at, source=TradeSource.BOT,
                external_position_id=o.position_id, fill_confirmed=True,
                external_fill_id=o.entry_order_id, account_mode=o.account_mode,
                entry_reason=f"Открыто из бота (#{o.id})",
            )
        except (JournalError, CalculationError):
            trade = await journal.open_trade(
                user_id=o.user_id, symbol=o.symbol, side=self.side, entry_price=o.avg_price,
                quantity=o.filled_qty, leverage=o.leverage, fee=o.entry_fee or ZERO,
                account_balance=o.equity, opened_at=o.filled_at, source=TradeSource.BOT,
                external_position_id=o.position_id, fill_confirmed=True,
                external_fill_id=o.entry_order_id, account_mode=o.account_mode,
                entry_reason=f"Открыто из бота (#{o.id})",
            )
        trade.initial_stop_loss = o.stop_loss
        if close_fill is not None:
            await journal.close_trade(
                trade, exit_price=close_fill.avg_price, fee=close_fill.fee,
                exit_reason=reason or EXIT_REASON_EMERGENCY,
                closed_at=close_fill.filled_at or datetime.now(UTC),
                external_fill_id=close_fill.order_id,
            )
        await self.session.execute(
            update(ExecutionOrder)
            .where(ExecutionOrder.trade_opening_id == o.id)
            .values(trade_id=trade.id)
        )
        o.trade_id = trade.id
        await self.session.commit()
        logger.info("Сделка открытия в журнале", extra={**self._log(), "trade_id": trade.id})
        return trade.id

    def summary(self) -> str:
        o = self.opening
        return (
            f"{o.symbol} {self.side.value} {fmt_qty(o.filled_qty, self.qp)} @ "
            f"{fmt_price(o.avg_price, self.pp)}"
        )

    def _log(self) -> dict[str, Any]:
        o = self.opening
        return {
            "user_id": o.user_id, "opening_id": o.id, "symbol": o.symbol,
            "side": self.side.value,
        }
