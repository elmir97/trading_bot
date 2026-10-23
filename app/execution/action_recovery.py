"""B.9 (19.10.2026): восстановление действий этапа 4 после рестарта.

Действие, прерванное посреди «Да» (SIGTERM, падение, OOM), остаётся
SUBMITTED (до B.9 — CARD) со строками execution_orders в PENDING /
SUBMITTED / UNKNOWN, закрытие — ещё FILLED без цены. Здесь его доводят до
итога — при старте бота до поллинга и каждым циклом reconciler до сверки
сделок (orderId закрытия должен быть в строке раньше, чем сверка разберёт
выход, иначе он запишется «вне бота»).

Решения владельца 18–19.10:
- биржа — ТОЛЬКО чтение (openOrders, позиции, ордер по cid, allOrders).
  Ордера без свежего «Да» не отправляются и не снимаются ни в одной ветке:
  лишний мост или старый стоп — сообщением «сними в BingX»;
- ордер — клиентом счёта действия (position_actions.account_mode, M9):
  109421 от клиента другого режима — тоже «order not exist»;
- «ордера нет» = 109421 по cid + нет в openOrders + нет в истории allOrders;
  NOT_PLACED — только после ДВУХ таких проверок в разных проходах, не
  раньше NOT_PLACED_RECHECK после первой (время первой — в Redis: переживает
  рестарт). Любой признак ордера между ними снимает отметку;
- сообщение в чат — итогом, через notify (бот: app/bot/action_result.py);
- строка старше окна истории BingX (HISTORY_WINDOW, 7 дней; владелец 19.10):
  одна проверка по cid — не решила (109421, сбой, не окончательный статус) —
  WARNING «нужна ручная сверка», error_code MANUAL_RECONCILE (у строки, у
  активного действия — и у него), и из проходов она исключена; закрывает
  владелец вручную по истории BingX.

Сервисный слой: тот же проход вызовет и API Mini App, когда действия
переедут туда. Под локом позиции, как «Да»: лок занят — пропуск до
следующего прохода.

Уже завершённые действия (EXPIRED/REFUSED/FAILED/DONE) с висящими строками —
только строки, без сообщения (#40/210, 12.10); исключение — закрытие,
которое всё же исполнилось: действие DONE и сообщение. FAILED «Исполнение
закрытия не подтверждено» с добранной строкой — DONE без сообщения, как у
добора 12.10 (app/execution/close_backfill.py — с B.9 только строки без
действия).
"""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.locks import LockBusyError, RedisLock, position_lock_key
from app.core.logging import get_logger
from app.core.numfmt import fmt_amount, fmt_price, fmt_qty
from app.database.models.execution_order import ExecutionOrder
from app.database.models.position_action import PositionAction
from app.database.models.trade import TradeFill
from app.database.models.user import User
from app.database.session import Database
from app.exchanges.base import ExchangeClient, ExchangeError, OpenOrder, OrderFill, Position
from app.execution.close_backfill import CONFIRMED_MARK, UNCONFIRMED_MESSAGE
from app.execution.models import action_client_order_id
from app.execution.position_action_service import journal_exit, settle_closed
from app.execution.position_actions import levels_after_partial
from app.execution.position_view import protective_orders
from app.execution.settle import (
    conditional_rows,
    exchange_cancelled,
    history_facts,
    history_window,
)
from app.market.cache import TTLCache
from app.market.data import MarketDataService
from app.trading.enums import (
    OrderRole,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionActionKind,
    PositionActionStatus,
    TradeSide,
)

logger = get_logger(__name__)

ORDER_NOT_EXIST_CODE = 109421
# Вторая отрицательная проверка засчитывается не раньше чем через столько
# после первой (и всегда в другом проходе: за проход строка проверяется раз).
NOT_PLACED_RECHECK = timedelta(seconds=30)
NOT_FOUND_KEY = "action:notfound:{row_id}"
# Отметка первой проверки живёт час (владелец 19.10): не копится; вторая
# проверка — через минуту, циклом reconciler.
NOT_FOUND_TTL_SECONDS = 3600
# Окно истории BingX (как BACKFILL_WINDOW добора): старше — «нет в истории»
# не доказать, опрашивать каждый цикл бессмысленно.
HISTORY_WINDOW = timedelta(days=7)
MANUAL_RECONCILE = "MANUAL_RECONCILE"
# Окно истории вокруг строки: POST уходит через миллисекунды после коммита.
HISTORY_BEFORE = timedelta(minutes=1)
HISTORY_AFTER = timedelta(hours=1)
MAX_ACTIONS_PER_PASS = 10
INTERRUPTED = "INTERRUPTED"
# Завершённое действие (EXPIRED/REFUSED/FAILED), чьё закрытие доказано по cid
# (владелец 19.10, отступление от правила 12.10 принято).
CLOSED_AFTER_ALL = "ранее было отмечено как неудачное, но закрытие исполнилось на бирже."
RECOVERED_MARK = "восстановление по cid"

_ACTIVE = (PositionActionStatus.CARD, PositionActionStatus.SUBMITTED)
_CLOSING_SIDE = {TradeSide.LONG: OrderSide.SELL, TradeSide.SHORT: OrderSide.BUY}
_GONE = frozenset({"CANCELLED", "CANCELED", "EXPIRED"})
_WHAT = {
    PositionActionKind.MOVE_STOP: "перенос стопа",
    PositionActionKind.SET_TAKE: "тейк",
    PositionActionKind.CLOSE_PARTIAL: "частичное закрытие",
    PositionActionKind.CLOSE_FULL: "закрытие",
}
_market_cache = TTLCache()


@dataclass(frozen=True, slots=True)
class RecoveryNote:
    """Итог восстановления для чата: новое сообщение; у карточки
    card_message_id снимаются кнопки (если она не итоговая)."""

    user_id: int
    telegram_id: int
    action_id: int
    card_message_id: int | None
    text: str


class Notify(Protocol):
    def __call__(self, note: RecoveryNote) -> Awaitable[None]: ...


class Seen(StrEnum):
    """Что видно на бирже по строке ордера действия."""

    STANDING = "STANDING"      # стоит в openOrders
    FILLED = "FILLED"          # исполнен (закрытие) / сработал (стоп, тейк)
    GONE = "GONE"              # снят или истёк без исполнения
    NOT_PLACED = "NOT_PLACED"  # две проверки «нет нигде»
    WAIT = "WAIT"              # первая проверка «нет нигде» — ждём вторую
    UNKNOWN = "UNKNOWN"        # биржа не ответила / ответ не доказательство
    MANUAL = "MANUAL"          # старше окна истории, проверка не решила — вручную


_UNDECIDED = (Seen.WAIT, Seen.UNKNOWN, Seen.MANUAL)


def _inflight(row: Any = ExecutionOrder) -> Any:
    """Строка, чей исход не записан (выражение SQL): PENDING/UNKNOWN; у
    закрытия ещё SUBMITTED и FILLED без цены. SUBMITTED стопа/тейка — норма
    (стоит на бирже), её разбирает settle."""
    return and_(
        row.client_order_id.is_not(None),
        row.error_code.is_distinct_from(MANUAL_RECONCILE),
        or_(
            row.status.in_((OrderStatus.PENDING, OrderStatus.UNKNOWN)),
            and_(row.role == OrderRole.CLOSE, row.status == OrderStatus.SUBMITTED),
            and_(row.role == OrderRole.CLOSE, row.status == OrderStatus.FILLED,
                 row.price.is_(None)),
        ),
    )


def _with_manual(code: str | None, message: str | None, note: str) -> str:
    """MANUAL_RECONCILE: прежнее не затирается (владелец 19.10) — сообщение
    дописывается, прежний error_code — в нём же."""
    tail = f"MANUAL_RECONCILE: {note}; прежний код: {code or '—'}."
    return f"{message} | {tail}" if message else tail


def _row_inflight(row: ExecutionOrder) -> bool:
    if row.client_order_id is None or row.error_code == MANUAL_RECONCILE:
        return False
    if row.status in (OrderStatus.PENDING, OrderStatus.UNKNOWN):
        return True
    return row.role is OrderRole.CLOSE and (
        row.status is OrderStatus.SUBMITTED
        or (row.status is OrderStatus.FILLED and row.price is None)
    )


def _row_open(row: ExecutionOrder) -> bool:
    """У активного действия — любая строка без окончательного исхода."""
    return _row_inflight(row) or (
        row.client_order_id is not None and row.status is OrderStatus.SUBMITTED
        and row.error_code != MANUAL_RECONCILE
    )


async def due_action_ids(session: AsyncSession, limit: int = MAX_ACTIONS_PER_PASS) -> list[int]:
    """SUBMITTED; CARD со строкой ордера (код до B.9); любое действие со
    строкой без записанного исхода. Отмеченные MANUAL_RECONCILE — нет."""
    rows = ExecutionOrder
    has_inflight = exists().where(rows.position_action_id == PositionAction.id, _inflight())
    has_order = exists().where(
        rows.position_action_id == PositionAction.id, rows.client_order_id.is_not(None)
    )
    return list(await session.scalars(
        select(PositionAction.id).where(
            PositionAction.error_code.is_distinct_from(MANUAL_RECONCILE),
            or_(
                PositionAction.status == PositionActionStatus.SUBMITTED,
                and_(PositionAction.status == PositionActionStatus.CARD, has_order),
                has_inflight,
            ),
        ).order_by(PositionAction.id).limit(limit)
    ))


async def recover_actions(
    db: Database, settings: Settings, redis: Any, factory: Any, notify: Notify,
    *, now: datetime | None = None,
) -> int:
    """Один проход. Возвращает число действий, у которых что-то записано."""
    async with db.session() as session:
        ids = await due_action_ids(session)
    moved = 0
    for action_id in ids:
        try:
            if await recover_one(db, settings, redis, factory, notify, action_id, now=now):
                moved += 1
        except Exception:
            # Одно действие не роняет остальные; повтор — следующим проходом.
            logger.exception("Восстановление действия упало", extra={"action_id": action_id})
    return moved


async def recover_one(
    db: Database, settings: Settings, redis: Any, factory: Any, notify: Notify,
    action_id: int, *, now: datetime | None = None,
) -> bool:
    moment = now or datetime.now(UTC)
    async with db.session() as session:
        action = await session.get(PositionAction, action_id)
        if action is None:
            return False
        user = await session.get(User, action.user_id)
        if user is None:
            return False
        key = position_lock_key(user.id, action.symbol, action.side.value)
        try:
            async with RedisLock(redis, key, settings.confirm_lock_ttl_seconds):
                await session.refresh(action)
                result = await _Pass(session, redis, factory, action, moment).run()
        except LockBusyError:
            logger.info("Восстановление действия пропущено: лок позиции занят",
                        extra={"action_id": action_id})
            return False
        if result is None:
            return False
        if result.text is not None:
            await notify(RecoveryNote(
                user_id=user.id, telegram_id=user.telegram_id, action_id=action.id,
                card_message_id=action.card_message_id, text=result.text,
            ))
        return True


@dataclass(frozen=True, slots=True)
class _Result:
    text: str | None   # None — в чат не писать


class _Pass:
    """Проход по одному действию под локом позиции."""

    def __init__(
        self, session: AsyncSession, redis: Any, factory: Any, action: PositionAction,
        now: datetime,
    ) -> None:
        self.session = session
        self.redis = redis
        self.factory = factory
        self.action = action
        self.now = now
        self.log: dict[str, Any] = {"action_id": action.id, "symbol": action.symbol,
                                    "account_mode": action.account_mode.value}
        self.fills: dict[int, OrderFill] = {}

    async def run(self) -> _Result | None:
        a = self.action
        rows = list(await self.session.scalars(
            select(ExecutionOrder).where(ExecutionOrder.position_action_id == a.id)
            .order_by(ExecutionOrder.id)
        ))
        active = a.status in _ACTIVE
        check = [r for r in rows if (_row_open(r) if active else _row_inflight(r))]
        if not check and not active:
            return None
        try:
            client = await self.factory.for_user(self.session, a.user_id, mode=a.account_mode)
        except ExchangeError as exc:
            logger.warning("Восстановление действия: клиента нет",
                           extra={**self.log, "error": type(exc).__name__})
            return None
        try:
            return await self._run(client, rows, check, active)
        except ExchangeError as exc:
            logger.info("Восстановление действия: биржа не ответила — повтор следующим проходом",
                        extra={**self.log, "error": type(exc).__name__})
            await self.session.rollback()
            return None
        finally:
            await client.close()

    async def _run(
        self, client: ExchangeClient, rows: list[ExecutionOrder], check: list[ExecutionOrder],
        active: bool,
    ) -> _Result | None:
        a = self.action
        orders = await client.get_open_orders(a.symbol)
        seen: dict[int, Seen] = {}
        for row in check:
            seen[row.id] = await self._resolve(client, row, orders)
        undecided = {rid: s.value for rid, s in seen.items() if s in _UNDECIDED}
        if Seen.MANUAL in seen.values() and active:
            # Действие не довести без истории — тоже из проходов, до ручной сверки.
            a.error_message = _with_manual(
                a.error_code, a.error_message,
                "строка ордера старше окна истории BingX — нужна ручная сверка",
            )
            a.error_code = MANUAL_RECONCILE
        if undecided:
            await self.session.commit()   # подтверждённое по другим строкам — не терять
            logger.info("Действие не восстановлено — повтор следующим проходом",
                        extra={**self.log, "rows": undecided})
            return None
        if not active:
            return await self._finished(rows, seen, client, orders)
        info = await MarketDataService(client, _market_cache).get_symbol_info(a.symbol)
        if info is None:
            logger.info("Восстановление действия: биржа не отдала параметры символа",
                        extra=self.log)
            await self.session.rollback()
            return None
        positions = await client.get_positions()
        position = next(
            (p for p in positions if p.symbol == a.symbol and p.side is a.side), None
        )
        placed = [r for r in rows if r.client_order_id is not None]
        if not placed:
            status, text = PositionActionStatus.FAILED, (
                "прервано перезапуском до отправки — на бирже ничего нет. Открой действие заново."
            )
        elif a.kind in (PositionActionKind.CLOSE_PARTIAL, PositionActionKind.CLOSE_FULL):
            status, text = await self._close_outcome(
                client, placed, seen, orders, position, info.price_precision,
                info.quantity_precision,
            )
        else:
            status, text = self._move_outcome(
                placed, orders, position, info.price_precision, info.quantity_precision,
            )
        return await self._decide(status, text)

    # --- строка ордера -------------------------------------------------------

    def _standing(self, row: ExecutionOrder, orders: list[OpenOrder]) -> OpenOrder | None:
        cid = (row.client_order_id or "").casefold()
        for o in orders:
            if row.exchange_order_id and str(o.order_id) == row.exchange_order_id:
                return o
            if cid and o.client_order_id.casefold() == cid:
                return o
        return None

    async def _resolve(
        self, client: ExchangeClient, row: ExecutionOrder, orders: list[OpenOrder]
    ) -> Seen:
        log = {**self.log, "execution_order_id": row.id, "cid": row.client_order_id,
               "prev_status": row.status.value}
        found = self._standing(row, orders)
        old = self.now - row.created_at > HISTORY_WINDOW
        if found is not None and row.role is not OrderRole.CLOSE:
            row.status = OrderStatus.SUBMITTED
            row.exchange_order_id = str(found.order_id)
            await self._clear(row)
            return Seen.STANDING
        try:
            fill = await client.get_order_fill(row.symbol, row.client_order_id or "",
                                               max_attempts=1)
        except ExchangeError as exc:
            if old:
                return self._manual(row, log, f"по cid: {type(exc).__name__} {exc.code or ''}")
            if exc.code != ORDER_NOT_EXIST_CODE:
                logger.info("Строка действия не проверена по cid",
                            extra={**log, "error": type(exc).__name__, "code": exc.code})
                return Seen.UNKNOWN
            if found is not None:
                return Seen.UNKNOWN   # стоит, а по cid «нет» — не доказательство
            return await self._not_found(client, row, log)
        await self._clear(row)
        if fill.status == "FILLED" and fill.executed_qty > 0:
            row.status = OrderStatus.FILLED
            row.exchange_order_id = fill.order_id or row.exchange_order_id
            if row.role is OrderRole.CLOSE:
                row.price = fill.avg_price
                row.quantity = fill.executed_qty
                row.raw_response = {
                    "status": fill.status, "avgPrice": str(fill.avg_price),
                    "executedQty": str(fill.executed_qty), "commission": str(fill.fee),
                    "source": RECOVERED_MARK,
                }
                self.fills[row.id] = fill
            logger.info("Строка действия: исполнена", extra={**log, "qty": str(fill.executed_qty)})
            return Seen.FILLED
        if fill.status in _GONE and fill.executed_qty == 0:
            row.status = OrderStatus.CANCELLED
            row.cancel_source = None   # точно не стоит, кто снял — неизвестно (M7)
            row.exchange_order_id = fill.order_id or row.exchange_order_id
            logger.info("Строка действия: снята без исполнения", extra=log)
            return Seen.GONE
        if old:
            return self._manual(row, log, f"по cid статус {fill.status}")
        logger.info("Строка действия: исход по cid не окончательный",
                    extra={**log, "status": fill.status, "executed": str(fill.executed_qty)})
        return Seen.UNKNOWN

    def _manual(self, row: ExecutionOrder, log: dict[str, Any], why: str) -> Seen:
        """Старше окна истории, одна проверка по cid не решила: отметка и
        WARNING — дальше строку проходы не берут (владелец 19.10)."""
        row.error_message = _with_manual(
            row.error_code, row.error_message,
            f"старше окна истории BingX ({HISTORY_WINDOW.days} дн.) — нужна ручная сверка; "
            f"{why.strip()}",
        )
        row.error_code = MANUAL_RECONCILE
        logger.warning("Строка действия старше окна истории BingX — нужна ручная сверка",
                       extra={**log, "why": why.strip(),
                              "age_days": (self.now - row.created_at).days})
        return Seen.MANUAL

    async def _not_found(
        self, client: ExchangeClient, row: ExecutionOrder, log: dict[str, Any]
    ) -> Seen:
        """109421 по cid и нет в openOrders: ещё история. «Нет нигде» —
        отрицательная проверка; NOT_PLACED — вторая, в другом проходе."""
        absent = await self._absent_in_history(client, row)
        if absent is not True:
            logger.info("Строка действия: история не доказывает «ордера нет»",
                        extra={**log, "history": "есть" if absent is False else "нет ответа"})
            return Seen.UNKNOWN
        key = NOT_FOUND_KEY.format(row_id=row.id)
        first = await self.redis.get(key)
        if first is None:
            await self.redis.set(key, self.now.timestamp(), ex=NOT_FOUND_TTL_SECONDS)
            logger.info("Строка действия: ордера нет — первая проверка", extra=log)
            return Seen.WAIT
        since = self.now.timestamp() - float(first)
        if since < NOT_PLACED_RECHECK.total_seconds():
            return Seen.WAIT
        row.status = OrderStatus.NOT_PLACED
        await self.redis.delete(key)
        logger.info("Строка действия: ордера нет — вторая проверка, NOT_PLACED",
                    extra={**log, "since_first_s": round(since, 1)})
        return Seen.NOT_PLACED

    async def _absent_in_history(self, client: ExchangeClient, row: ExecutionOrder) -> bool | None:
        """True — в истории вокруг строки ордера с этим cid нет; False — есть;
        None — не проверить (сбой, у элемента нет clientOrderId)."""
        start = row.created_at - HISTORY_BEFORE
        end = max(min(self.now, row.created_at + HISTORY_AFTER), start + timedelta(seconds=1))
        try:
            history = await client.get_all_orders(row.symbol, start, end, max_attempts=1)
        except (ExchangeError, ValueError) as exc:
            logger.info("Строка действия: история не получена",
                        extra={**self.log, "execution_order_id": row.id,
                               "error": type(exc).__name__})
            return None
        cid = (row.client_order_id or "").casefold()
        if any(h.client_order_id is None for h in history):
            return None
        return not any(h.client_order_id.casefold() == cid for h in history if h.client_order_id)

    async def _clear(self, row: ExecutionOrder) -> None:
        await self.redis.delete(NOT_FOUND_KEY.format(row_id=row.id))

    # --- итог действия --------------------------------------------------------

    async def _decide(self, status: PositionActionStatus, text: str) -> _Result:
        a = self.action
        before = a.status
        a.status = status
        a.decided_at = a.decided_at or self.now
        if status is PositionActionStatus.FAILED:
            a.error_code = INTERRUPTED
            a.error_message = text
        await self.session.commit()
        logger.info("Действие восстановлено",
                    extra={**self.log, "from": before.value, "to": status.value})
        return _Result(f"♻️ После перезапуска бота — {_WHAT[a.kind]} {a.symbol} "
                       f"{a.side.value}:\n{text}")

    async def _finished(
        self, rows: list[ExecutionOrder], seen: dict[int, Seen], client: ExchangeClient,
        orders: list[OpenOrder],
    ) -> _Result:
        """Действие уже завершено: строки записаны. Закрытие исполнилось, а
        действие не DONE — DONE (FAILED «не подтверждено» — молча, как добор)."""
        a = self.action
        filled = [r for r in rows if seen.get(r.id) is Seen.FILLED and r.role is OrderRole.CLOSE]
        if not filled or a.status is PositionActionStatus.DONE:
            await self.session.commit()
            logger.info("Строки завершённого действия разобраны",
                        extra={**self.log, "status": a.status.value,
                               "rows": {rid: s.value for rid, s in seen.items()}})
            return _Result(None)
        silent = (
            a.status is PositionActionStatus.FAILED and a.error_message == UNCONFIRMED_MESSAGE
        )
        before = a.status
        a.status = PositionActionStatus.DONE
        a.error_code = None
        a.error_message = None
        a.params = {**(a.params or {}), "confirmed_by": CONFIRMED_MARK if silent
                    else RECOVERED_MARK, "confirmed_at": self.now.isoformat()}
        await self._journal(filled[0])
        await self.session.commit()
        logger.info("Действие подтверждено добором" if silent else "Действие восстановлено",
                    extra={**self.log, "from": before.value, "to": a.status.value})
        if silent:
            return _Result(None)
        info = await MarketDataService(client, _market_cache).get_symbol_info(a.symbol)
        fill = self.fills[filled[0].id]
        pp = info.price_precision if info else None
        qp = info.quantity_precision if info else 8
        return _Result(
            f"♻️ После перезапуска бота — {_WHAT[a.kind]} {a.symbol} {a.side.value}: "
            f"действие #{a.id} {CLOSED_AFTER_ALL}\n"
            f"✅ Закрыто {fmt_qty(fill.executed_qty, qp)} по {fmt_price(fill.avg_price, pp)} · "
            f"комиссия {fmt_amount(fill.fee)} USDT."
        )

    async def _journal(self, row: ExecutionOrder) -> None:
        """Выход в журнал тем же путём, что «Да»; уже записанный (сверка
        успела по orderId) — не повторять."""
        a = self.action
        fill = self.fills.get(row.id)
        if a.trade_id is None or fill is None:
            return
        recorded = await self.session.scalar(select(TradeFill.id).where(
            TradeFill.trade_id == a.trade_id, TradeFill.external_fill_id == str(fill.order_id)
        ))
        if recorded is None:
            await journal_exit(self.session, a.trade_id, fill,
                               a.kind is PositionActionKind.CLOSE_FULL)

    async def _close_outcome(
        self, client: ExchangeClient, placed: list[ExecutionOrder], seen: dict[int, Seen],
        orders: list[OpenOrder], position: Position | None, pp: int, qp: int,
    ) -> tuple[PositionActionStatus, str]:
        a = self.action
        [row] = [r for r in placed if r.role is OrderRole.CLOSE] or [placed[0]]
        state = seen.get(row.id)
        if state is Seen.NOT_PLACED:
            return PositionActionStatus.FAILED, (
                "закрытие не было отправлено на биржу (проверено по clientOrderId дважды). "
                "Позиция не тронута — открой действие заново."
            )
        if state is Seen.GONE:
            return PositionActionStatus.FAILED, (
                "биржа сняла закрытие без исполнения — позиция не тронута. Проверь BingX."
            )
        fill = self.fills.get(row.id)
        if state is Seen.FILLED and fill is not None:
            await self._journal(row)
            text = (f"✅ Закрыто {fmt_qty(fill.executed_qty, qp)} по "
                    f"{fmt_price(fill.avg_price, pp)} · комиссия {fmt_amount(fill.fee)} USDT.")
        elif state is None and row.status is OrderStatus.FILLED and row.price is not None:
            # Дочитано «Да» до обрыва (строка полная) — выход в журнал допишет
            # сверка по orderId строки.
            text = f"✅ Закрыто {fmt_qty(row.quantity, qp)} по {fmt_price(row.price, pp)}."
        else:
            return PositionActionStatus.FAILED, (
                f"исход закрытия не установлен ({state.value if state else row.status.value}) — "
                "проверь позицию в BingX."
            )
        closing = _CLOSING_SIDE[a.side].value
        if a.kind is PositionActionKind.CLOSE_PARTIAL:
            stops, takes = protective_orders(position, orders) if position else ((), ())
            rest = position.quantity if position else Decimal(0)
            return PositionActionStatus.DONE, (
                f"{text}\n{levels_after_partial(stops, takes, rest, qp)}."
            )
        await self._settle_after_close(client, row, {str(o.order_id) for o in orders})
        leftovers = sorted(
            str(o.order_id) for o in orders
            if o.position_side == a.side.value and o.side == closing and o.stop_price is not None
        )
        if leftovers:
            return PositionActionStatus.DONE, (
                f"{text}\n⚠️ На бирже остались условные ордера: {', '.join(leftovers)} — "
                "сними их в BingX."
            )
        return PositionActionStatus.DONE, f"{text}\nСтоп и тейк биржа сняла сама."

    async def _settle_after_close(
        self, client: ExchangeClient, close_row: ExecutionOrder, live: set[str]
    ) -> None:
        """Строки стопов/тейков закрытой позиции — по openOrders и истории
        (п.1б, 24.10): EXCHANGE — отмена в допуске от updateTime нашего
        закрытия (по orderId строки), иначе NULL. Окно — от создания самой
        ранней строки (allOrders отбирает по созданию); окно неполное или
        история не получена — «нет в истории» строку не закрывает."""
        a = self.action
        rows = await conditional_rows(
            self.session, user_id=a.user_id, trade_id=a.trade_id, position_id=a.position_id,
        )
        if not rows:
            return
        window = history_window([close_row.created_at, *(r.created_at for r in rows)], self.now)
        try:
            history = await client.get_all_orders(a.symbol, window.start, self.now,
                                                  max_attempts=1)
        except (ExchangeError, ValueError) as exc:
            logger.info("Восстановление действия: история для строк стопа/тейка не получена",
                        extra={**self.log, "error": type(exc).__name__})
            history, complete = [], False
        else:
            complete = window.complete(history)
        await settle_closed(
            self.session, a, live, rows=rows, facts=history_facts(history),
            default=OrderStatus.CANCELLED if complete else None,
            exchange_ids=exchange_cancelled(history, close_row.exchange_order_id),
        )

    def _move_outcome(
        self, placed: list[ExecutionOrder], orders: list[OpenOrder], position: Position | None,
        pp: int, qp: int,
    ) -> tuple[PositionActionStatus, str]:
        """По тому, что стоит СЕЙЧАС (openOrders): итоговый closePosition-ордер,
        мост (…SB/…TB), старый ордер того же типа."""
        a = self.action
        is_stop = a.kind is PositionActionKind.MOVE_STOP
        name = "стоп" if is_stop else "тейк"
        title = name.capitalize()
        two = "два стопа" if is_stop else "два тейка"
        role = OrderRole.STOP_LOSS if is_stop else OrderRole.TAKE_PROFIT
        otype = (OrderType.STOP_MARKET if is_stop else OrderType.TAKE_PROFIT_MARKET).value
        final_cid = action_client_order_id(action_id=a.id, user_id=a.user_id, role=role)
        bridge_cid = action_client_order_id(action_id=a.id, user_id=a.user_id, role=role,
                                            bridge=True)
        by_cid = {(r.client_order_id or "").casefold(): r for r in placed}
        final_row = by_cid.get(final_cid.casefold())
        bridge_row = by_cid.get(bridge_cid.casefold())
        final = self._standing(final_row, orders) if final_row is not None else None
        bridge = self._standing(bridge_row, orders) if bridge_row is not None else None
        ours = {str(o.order_id) for o in (final, bridge) if o is not None} | {
            r.exchange_order_id for r in placed if r.exchange_order_id
        }
        closing = _CLOSING_SIDE[a.side].value
        old = sorted(
            (o for o in orders
             if o.order_type == otype and o.position_side == a.side.value and o.side == closing
             and str(o.order_id) not in ours
             and o.client_order_id.casefold() not in (final_cid.casefold(), bridge_cid.casefold())),
            key=lambda o: str(o.order_id),
        )
        new_s = fmt_price(a.new_level, pp)
        old_level = a.current_stop if is_stop else a.current_take
        old_s = fmt_price(old_level, pp) if old_level is not None else None
        done, failed = PositionActionStatus.DONE, PositionActionStatus.FAILED
        if final_row is not None and final_row.status is OrderStatus.FILLED and final is None:
            return done, f"✅ {title} {new_s} встал и уже сработал — итог сделки пришлёт сверка."
        if final is not None and bridge is None and not old:
            head = f"{title} {old_s} → {new_s}" if old_s else f"{title} {new_s}"
            return done, f"✅ {head} (на всю позицию)."
        if final is not None and bridge is not None:
            return failed, (
                f"{title} {new_s} на всю позицию стоит, но промежуточный {bridge.order_id} "
                f"на той же цене не снят — на позиции {two}. Сними его в BingX."
            )
        if final is not None:
            ids = ", ".join(str(o.order_id) for o in old)
            return failed, (
                f"{title} {new_s} на всю позицию стоит, но старый ({ids}) тоже — на позиции {two}. "
                "Сними старый в BingX."
            )
        if bridge is not None and old:
            return failed, (
                f"перенос прерван: стоят старый {name} {old_s or old[0].stop_price} и "
                f"промежуточный {new_s} на {fmt_qty(bridge.quantity, qp)} — на позиции {two}. "
                "Сними лишний в BingX."
            )
        if bridge is not None:
            head = f"{title} {old_s} → {new_s}" if old_s else f"{title} {new_s}"
            return failed, (
                f"{head} стоит промежуточным ордером на {fmt_qty(bridge.quantity, qp)} "
                "(не «на всю позицию»). Проверь BingX."
            )
        if old:
            return failed, f"перенос не выполнен — на бирже всё как было: {name} {old_s}."
        if position is None:
            return failed, f"позиции {a.symbol} {a.side.value} на бирже уже нет."
        if is_stop:
            return failed, "🚨 на позиции нет стопа — поставь его через «⚙️ Действия» или в BingX."
        return failed, "тейк не встал — тейка на позиции нет."
