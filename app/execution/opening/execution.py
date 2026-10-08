"""Исполнение открытия: вход, read-back, защита, журнал (§5 шаги 7–11 плана).

Общий для «Открыть» (service.confirm) и восстановления после рестарта
(recovery): каждый шаг читает состояние из БД и биржи и ничего не делает
дважды. Новый вход отправляется только из confirm и только один раз —
строка ENTRY в execution_orders (UNIQUE client_order_id) коммитится ДО HTTP.

Защита позиции (разведка Р3/Р4/Р6, 05.10; замена — решение владельца 08.10):
1. вложенные стоп и тейк ищутся в openOrders по positionID, типу, стороне
   закрытия и цене (clientOrderId у них пустой) — они на объём входа, не
   closePosition;
2. сразу после входа они заменяются на closePosition по той же цене: новый
   (cid …s{n} / …t{n}, closePosition + quantity = объём позиции, правило
   разведки 02.10) подтверждён → вложенный снят по orderId → снятие
   подтверждено повторным openOrders. Позиция под стопом на каждом шаге;
3. closePosition-стоп не встал, а вложенный покрывает позицию — остаётся
   вложенный, предупреждение; вложенного нет — аварийное закрытие маркетом,
   сделка в журнал с выходом;
4. закрытие не прошло — ALARM: тревога и повтор циклом восстановления.
Тейк — так же, но без аварии: не встал — «тейк не стоит».

«Стоп не встал» (08.10.2026, решение владельца) — только окончательно: POST
отклонён кодом биржи ИЛИ ордер по своему clientOrderId не найден (109421) /
отменён. Нет в openOrders, а по cid ответа нет — «не подтверждён»: ALARM без
закрытия, перепроверка следующим циклом. Перед новым запасным стопом в цикле
тревоги прежние (реально отправленные) проверяются по cid.

Управляемый сбой EXEC_OPEN_FAULT (только демо, config.OPEN_FAULTS) — подмена
реакции бота на первой попытке в открытии; реальные ордера уходят как есть.
Ликвидация по факту между входом и стопом — тоже аварийное закрытие.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
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
    OrderNotFoundError,
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
FAULT_CODE = "FAULT"
# Статусы ордера по clientOrderId: стоит (или уже сработал) / снят.
_STANDING = frozenset({"NEW", "PENDING", "PARTIALLY_FILLED", "FILLED", "TRIGGERED"})
_GONE = frozenset({"CANCELLED", "CANCELED", "EXPIRED", "REJECTED", "FAILED"})


class Mode(StrEnum):
    REPLACED = "replaced"
    BACKUP = "backup"
    EXISTING = "existing"
    KEPT = "kept"


class StopCheck(StrEnum):
    STANDING = "STANDING"   # стоит на бирже — подтверждено
    ABSENT = "ABSENT"       # окончательно нет: POST отклонён / по cid не найден или снят
    UNKNOWN = "UNKNOWN"     # не подтверждён: перепроверить следующим циклом

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
    # Как стоит стоп/тейк (Mode): replaced — вложенный заменён на closePosition,
    # backup — вложенного не было, поставлен closePosition, existing — уже стоял
    # closePosition, kept — замена не прошла, стоит вложенный на объём входа.
    stop_mode: str = ""
    take_mode: str = ""
    warnings: tuple[str, ...] = ()
    close_fill: OrderFill | None = None
    reason: str = ""
    # ALARM без закрытия: стоп не подтверждён (не найден в openOrders, по cid
    # ответа нет) — перепроверка циклом, позиция не закрывается.
    undecided: bool = False


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
        self._faults = settings.exec_open_faults
        # hide_backup_stop: cid, скрытые в openOrders и в запросе по cid в
        # этом проходе (новый Runner в следующем цикле их уже видит).
        self._hidden: set[str] = set()

    @property
    def side(self) -> TradeSide:
        return self.opening.side

    def cid(self, letter: str, n: int = 0) -> str:
        return opening_client_order_id(self.opening.id, self.opening.user_id, letter, n)

    def _fault(self, name: str, attempt: int = 1) -> bool:
        """Управляемый сбой — только на первой попытке в открытии."""
        if name not in self._faults or attempt != 1:
            return False
        logger.warning(
            "Управляемый сбой: %s", name, extra={**self._log(), "fault": name}
        )
        return True

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
        stop: TpSlSpec | None = TpSlSpec(trigger_price=o.stop_loss)
        if self._fault("skip_attached_stop"):
            stop = None
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

    def _ours(
        self, orders: list[OpenOrder], order_type: OrderType, level: Decimal, position: Position
    ) -> list[OpenOrder]:
        """Наши стопы/тейки на позиции по цене: вложенные (на объём входа или
        часть лимита) и closePosition."""
        target = self._q(level)
        return [
            o for o in orders
            if o.symbol == self.opening.symbol and o.order_type == order_type.value
            and o.position_side == self.side.value
            and o.side == CLOSING_SIDE[self.side].value
            and o.stop_price is not None and self._q(o.stop_price) == target
            and (o.position_id is None or position.position_id is None
                 or o.position_id == position.position_id)
        ]

    def _match(
        self, orders: list[OpenOrder], order_type: OrderType, level: Decimal, position: Position
    ) -> OpenOrder | None:
        """Лучший из наших: closePosition (весь остаток), иначе наибольший объём."""
        candidates = self._ours(orders, order_type, level, position)
        if not candidates:
            return None
        return max(candidates, key=lambda o: (o.close_position, o.quantity))

    async def _visible_orders(self) -> list[OpenOrder]:
        orders = await self.client.get_open_orders(self.opening.symbol, max_retries=1)
        if not self._hidden:
            return orders
        return [o for o in orders if o.client_order_id.casefold() not in self._hidden]

    async def check_by_cid(self, cid: str) -> tuple[StopCheck, str | None]:
        """Ордер по своему clientOrderId: стоит / окончательно нет / неизвестно."""
        if cid.casefold() in self._hidden:
            logger.warning(
                "Управляемый сбой: стоп скрыт и в запросе по cid", extra={**self._log(), "cid": cid}
            )
            return StopCheck.ABSENT, None
        try:
            fill = await self.client.get_order_fill(self.opening.symbol, cid, max_retries=1)
        except ExchangeError as exc:
            if exc.code == ORDER_NOT_EXIST_CODE:
                return StopCheck.ABSENT, None
            return StopCheck.UNKNOWN, None
        if fill.status in _STANDING:
            return StopCheck.STANDING, fill.order_id
        if fill.status in _GONE:
            return StopCheck.ABSENT, fill.order_id
        return StopCheck.UNKNOWN, fill.order_id

    async def position(self) -> Position | None:
        positions = await self.client.get_positions(max_retries=1)
        return next(
            (p for p in positions if p.symbol == self.opening.symbol and p.side is self.side),
            None,
        )

    async def _record_conditional(self, role: OrderRole, order: OpenOrder) -> None:
        for row in await self._rows(role):
            if row.exchange_order_id == order.order_id or (
                order.client_order_id and row.client_order_id
                and row.client_order_id.casefold() == order.client_order_id.casefold()
            ):
                # Уже записан (в т.ч. запасной, сочтённый ненайденным в прошлом
                # проходе, — он стоит): подтверждаем строку.
                if row.status is not OrderStatus.SUBMITTED:
                    row.status = OrderStatus.SUBMITTED
                    row.exchange_order_id = order.order_id
                    await self.session.commit()
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
        stops: list[OpenOrder] = []
        takes: list[OpenOrder] = []
        for attempt in range(PROTECT_ATTEMPTS):
            if attempt:
                await self._sleep(self.settings.exec_order_readback_delay_ms / 1000)
            orders = await self._visible_orders()
            stops = self._ours(orders, OrderType.STOP_MARKET, o.stop_loss, position)
            if o.take_profit is not None:
                takes = self._ours(orders, OrderType.TAKE_PROFIT_MARKET, o.take_profit, position)
            if stops and (o.take_profit is None or takes):
                break
        warnings: list[str] = []
        stop_mode = await self._ensure_close_position(
            OrderRole.STOP_LOSS, o.stop_loss, stops, position, warnings
        )
        if stop_mode is StopCheck.UNKNOWN:
            return ProtectResult(
                OpeningStatus.ALARM, undecided=True,
                reason="стоп не подтверждён — перепроверка следующим циклом",
            )
        if stop_mode is StopCheck.ABSENT:
            return await self._emergency(position, EXIT_REASON_EMERGENCY)
        take_mode: Mode | StopCheck | None = None
        if o.take_profit is not None:
            take_mode = await self._ensure_close_position(
                OrderRole.TAKE_PROFIT, o.take_profit, takes, position, warnings
            )
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
            take_missing=o.take_profit is not None and not isinstance(take_mode, Mode),
            stop_mode=str(stop_mode),
            take_mode=str(take_mode) if isinstance(take_mode, Mode) else "",
            warnings=tuple(warnings),
        )

    async def _ensure_close_position(
        self, role: OrderRole, level: Decimal, found: list[OpenOrder], position: Position,
        warnings: list[str],
    ) -> Mode | StopCheck:
        """Стоп или тейк «на всю позицию» (closePosition) по цене level.

        Mode — стоит (как именно); StopCheck.ABSENT — окончательно нет и
        вложенного, покрывающего позицию, нет; StopCheck.UNKNOWN — не
        подтверждён (перепроверка циклом)."""
        is_stop = role is OrderRole.STOP_LOSS
        name = "стоп" if is_stop else "тейк"
        for order in found:
            await self._record_conditional(role, order)
        close = next((x for x in found if x.close_position), None)
        attached = [x for x in found if not x.close_position]
        covering = any(a.quantity >= position.quantity for a in attached)
        if close is not None:
            mode = Mode.EXISTING
        else:
            # Прежние closePosition-попытки, реально ушедшие на биржу, — сначала
            # по cid: в openOrders их может ещё не быть (принят, но не виден).
            earlier = await self._earlier_close(role)
            if earlier is StopCheck.STANDING:
                mode = Mode.EXISTING
            elif earlier is StopCheck.UNKNOWN:
                if covering:
                    warnings.append(
                        f"Замена {name}а на «всю позицию» не подтверждена — стоит вложенный "
                        f"{name} на {fmt_qty(position.quantity, self.qp)}."
                    )
                    return Mode.KEPT
                return StopCheck.UNKNOWN
            else:
                placed = await self._place_close(role, level, position)
                if placed is StopCheck.STANDING:
                    mode = Mode.REPLACED if attached else Mode.BACKUP
                elif covering:
                    warnings.append(
                        f"{name.capitalize()} «на всю позицию» не встал — стоит вложенный {name} "
                        f"на {fmt_qty(position.quantity, self.qp)} (биржа уменьшает его при "
                        "частичном закрытии, но не увеличивает при добавке к позиции)."
                    )
                    return Mode.KEPT
                else:
                    return placed
        left = await self._cancel_attached(role, attached)
        if left:
            warnings.append(
                f"Вложенный {name} на {fmt_qty(left[0].quantity, self.qp)} не снялся — на "
                f"позиции два {name}а на одной цене. Сними лишний в BingX."
            )
        if mode is Mode.EXISTING and attached:
            mode = Mode.REPLACED
        return mode

    async def _cancel_attached(self, role: OrderRole, attached: list[OpenOrder]) -> list[OpenOrder]:
        """Снять вложенные по orderId; снятие — только по повторному openOrders
        (ответ отмены не доказательство). Возвращает те, что остались."""
        if not attached:
            return []
        for order in attached:
            try:
                await self.client.cancel_order(self.opening.symbol, order.order_id)
            except OrderNotFoundError:
                pass
            except ExchangeError as exc:
                logger.warning(
                    "Вложенный ордер не снят", extra={**self._log(), "code": exc.code}
                )
        await self._sleep(self.settings.exec_order_readback_delay_ms / 1000)
        live = {o.order_id for o in await self.client.get_open_orders(
            self.opening.symbol, max_retries=1
        )}
        left = [a for a in attached if a.order_id in live]
        for row in await self._rows(role):
            if row.exchange_order_id in {a.order_id for a in attached} and (
                row.exchange_order_id not in live
            ):
                row.status = OrderStatus.CANCELLED
        await self.session.commit()
        if not left:
            logger.info(
                "Вложенный ордер заменён на closePosition",
                extra={**self._log(), "role": role.value, "count": len(attached)},
            )
        return left

    async def _attempt_no(self, role: OrderRole) -> int:
        count = await self.session.scalar(
            select(func.count()).select_from(ExecutionOrder).where(
                ExecutionOrder.trade_opening_id == self.opening.id,
                ExecutionOrder.role == role,
                ExecutionOrder.client_order_id.is_not(None),
            )
        )
        return int(count or 0) + 1

    async def _earlier_close(self, role: OrderRole) -> StopCheck | None:
        """closePosition-ордера прошлых проходов, реально отправленные (не FAULT и
        не отклонённые кодом биржи): стоит хоть один — STANDING; про какой-то нет
        ответа — UNKNOWN; иначе None (ставить новый)."""
        unknown = False
        for row in reversed(await self._rows(role)):
            if not row.client_order_id or row.error_code == FAULT_CODE:
                continue
            if row.status is OrderStatus.REJECTED and row.error_code not in (None, "NOT_FOUND"):
                continue   # отклонён биржей с кодом — не вставал
            check, order_id = await self.check_by_cid(row.client_order_id)
            if check is StopCheck.STANDING:
                row.status = OrderStatus.SUBMITTED
                row.exchange_order_id = order_id or row.exchange_order_id
                await self.session.commit()
                logger.info(
                    "closePosition прошлого прохода стоит — новый не ставлю",
                    extra={**self._log(), "cid": row.client_order_id},
                )
                return StopCheck.STANDING
            if check is StopCheck.UNKNOWN:
                unknown = True
        return StopCheck.UNKNOWN if unknown else None

    async def _place_close(
        self, role: OrderRole, level: Decimal, position: Position
    ) -> StopCheck:
        """closePosition-стоп/тейк на level: POST → openOrders → по cid.
        Сбои EXEC_OPEN_FAULT (fail/hide_backup_stop) — только для стопа."""
        o = self.opening
        is_stop = role is OrderRole.STOP_LOSS
        order_type = OrderType.STOP_MARKET if is_stop else OrderType.TAKE_PROFIT_MARKET
        n = await self._attempt_no(role)
        cid = self.cid("s" if is_stop else "t", n)
        row = self._row(
            role, order_type, CLOSING_SIDE[self.side],
            client_order_id=cid, quantity=position.quantity, trigger_price=level,
            status=OrderStatus.PENDING, stage="confirm",
        )
        self.session.add(row)
        await self.session.commit()
        logger.info(
            "Ставлю closePosition", extra={**self._log(), "role": role.value, "cid": cid}
        )
        if is_stop and self._fault("fail_backup_stop", n):
            row.status = OrderStatus.REJECTED
            row.error_code = FAULT_CODE
            row.error_message = "управляемый сбой fail_backup_stop: не отправлен"
            await self.session.commit()
            return StopCheck.ABSENT
        try:
            result = await self.client.place_conditional_order(
                symbol=o.symbol, side=CLOSING_SIDE[self.side], position_side=self.side.value,
                order_type=order_type.value, stop_price=level,
                quantity=position.quantity, client_order_id=cid, close_position=True,
            )
        except ExchangeError as exc:
            if not isinstance(exc, ExchangeUnavailableError) and exc.code is not None:
                # POST отклонён кодом биржи — стоп окончательно не встал.
                row.status = OrderStatus.REJECTED
                row.error_code = str(exc.code)
                row.error_message = str(exc)
                await self.session.commit()
                logger.error(
                    "closePosition отклонён биржей",
                    extra={**self._log(), "role": role.value, "code": exc.code},
                )
                return StopCheck.ABSENT
            # Без ответа ордер мог встать — решает чтение ниже.
            row.status = OrderStatus.UNKNOWN
            row.error_code = type(exc).__name__
            await self.session.commit()
        else:
            row.exchange_order_id = result.order_id or None
        if is_stop and self._fault("hide_backup_stop", n):
            self._hidden.add(cid.casefold())
        orders = await self._visible_orders()
        found = next(
            (x for x in orders if x.client_order_id.casefold() == cid.casefold()), None
        ) or next(
            (x for x in self._ours(orders, order_type, level, position) if x.close_position), None
        )
        if found is not None:
            row.status = OrderStatus.SUBMITTED
            row.exchange_order_id = found.order_id
            await self.session.commit()
            return StopCheck.STANDING
        # Нет в openOrders — решение только по запросу ордера по cid.
        check, order_id = await self.check_by_cid(cid)
        if check is StopCheck.STANDING:
            row.status = OrderStatus.SUBMITTED
            row.exchange_order_id = order_id or row.exchange_order_id
        elif check is StopCheck.ABSENT:
            row.status = OrderStatus.REJECTED
            row.error_code = "NOT_FOUND"
            row.error_message = "нет в openOrders, по clientOrderId не найден или снят"
            logger.error(
                "closePosition не встал: по cid не найден",
                extra={**self._log(), "role": role.value},
            )
        else:
            row.status = OrderStatus.UNKNOWN
            logger.warning(
                "closePosition не подтверждён — перепроверю",
                extra={**self._log(), "role": role.value},
            )
        await self.session.commit()
        return check

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
        if self._fault("fail_emergency_close", n):
            row.status = OrderStatus.REJECTED
            row.error_code = FAULT_CODE
            row.error_message = "управляемый сбой fail_emergency_close: не отправлено"
            await self.session.commit()
            return ProtectResult(OpeningStatus.ALARM, reason=reason)
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
