"""Стоп/тейк-строки execution_orders закрытой позиции — в FILLED/CANCELLED
по факту биржи (очередь A.3, 10.10.2026).

До A.3 строки стопа и тейка оставались SUBMITTED после «Закрыть всё»,
аварийного закрытия открытия и ручных позиций этапа 4 — SUBMITTED у ордера,
которого на бирже нет, ложь для settle и Mini App (CLAUDE.md, «Конвенции»).

Правило одно: строка закрывается, только если её orderId нет в свежем
openOrders. Снимок openOrders — обязательный аргумент settle(): без чтения
биржи строку не тронуть. Стоящий ордер остаётся как есть, решать вызывающему
(WARNING, перепроверка). Выбор строк — по сделке, открытию или positionId
биржи, никогда по символу: у execution_orders нет счёта, и строки той же
монеты на другом счёте (DEMO/LIVE) были бы задеты.

Кроме SUBMITTED/UNKNOWN — REJECTED с orderId: биржа ордер приняла (Т4: стоп
встал, но по cid «не найден»), статус ставится по факту, error_code остаётся
историей решения (решение владельца 10.10).

cancel_source (24.10.2026, очередь п.1б): по умолчанию NULL — «точно не
стоит, кто снял — неизвестно». EXCHANGE — только orderId из exchange_ids,
которые вызывающий доказал: стоял перед нашим закрытием (openOrders под
локом «Да», проход открытия) или снят в допуске от закрытия по истории
(exchange_cancelled). Без доказательства «биржа сняла» не пишем."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.database.models.execution_order import ExecutionOrder
from app.database.models.position_action import PositionAction
from app.exchanges.base import ALL_ORDERS_LIMIT, HistoryOrder
from app.trading.enums import CancelSource, OrderRole, OrderStatus

logger = get_logger(__name__)

CONDITIONAL_ROLES = (OrderRole.STOP_LOSS, OrderRole.TAKE_PROFIT)
OPEN_STATUSES = (OrderStatus.SUBMITTED, OrderStatus.UNKNOWN)
_FILLED = frozenset({"FILLED"})
_GONE = frozenset({"CANCELLED", "CANCELED", "EXPIRED"})

# Окно allOrders: не длиннее 7 дней (минута запаса на часы) и отбор — по
# времени СОЗДАНИЯ ордера (разведка 23.10: стоп/тейк, поставленные раньше
# начала окна, в ответ не попали). Поэтому окно — от создания самой ранней
# проверяемой строки минус запас, а не от закрытия.
HISTORY_LIMIT = timedelta(days=7) - timedelta(minutes=1)
HISTORY_MARGIN = timedelta(minutes=1)
# Допуск «биржа сняла при закрытии» (разведка 23.10, демо, XRP #36): стоп и
# тейк сняты через +1 с после updateTime закрывающего ордера, мост снят
# владельцем за 139 с до него. Точность time/updateTime на демо — целые
# секунды; для live перепроверить на первом закрытии. Известное
# ограничение (владелец 23.10): ручная отмена в ту же секунду, что и
# закрытие, получит EXCHANGE.
EXCHANGE_CANCEL_BEFORE = timedelta(seconds=1)
EXCHANGE_CANCEL_AFTER = timedelta(seconds=10)


@dataclass(frozen=True, slots=True)
class HistoryWindow:
    start: datetime
    # Начало не обрезано лимитом 7 дней: создание всех строк в окне.
    covers: bool

    def complete(self, history: Sequence[HistoryOrder]) -> bool:
        """Отсутствие в истории значит «ордера нет», только если окно
        покрывает создание строк и выдача не упёрлась в лимит ордеров."""
        return self.covers and len(history) < ALL_ORDERS_LIMIT


def history_window(created: Iterable[datetime], now: datetime) -> HistoryWindow:
    """Окно истории от создания самой ранней строки минус запас; старше
    7 дней — обрезано и неполное."""
    start = min(created) - HISTORY_MARGIN
    floor = now - HISTORY_LIMIT
    return HistoryWindow(max(start, floor), start >= floor)


def exchange_cancelled(
    history: Iterable[HistoryOrder], closing_order_id: str | None
) -> frozenset[str]:
    """orderId, снятые биржей при закрытии: отмена в [T − 1 с, T + 10 с], T —
    updateTime НАШЕГО закрывающего ордера, найденного в той же выдаче по
    orderId из БД. Нет его в выдаче (или orderId неизвестен) — пусто:
    первый попавшийся MARKET FILLED не берём."""
    orders = list(history)
    closing = next(
        (h for h in orders if closing_order_id and h.order_id == closing_order_id), None
    )
    if closing is None:
        return frozenset()
    t = closing.updated_at
    return frozenset(
        h.order_id for h in orders
        if h.status in _GONE
        and t - EXCHANGE_CANCEL_BEFORE <= h.updated_at <= t + EXCHANGE_CANCEL_AFTER
    )


@dataclass(slots=True)
class SettleResult:
    settled: list[int] = field(default_factory=list)
    standing: list[int] = field(default_factory=list)
    undecided: list[int] = field(default_factory=list)


def settleable(row: ExecutionOrder, *, include_rejected: bool = True) -> bool:
    if row.role not in CONDITIONAL_ROLES or not row.exchange_order_id:
        return False
    if row.status in OPEN_STATUSES:
        return True
    return include_rejected and row.status is OrderStatus.REJECTED


async def conditional_rows(
    session: AsyncSession,
    *,
    user_id: int,
    trade_id: int | None = None,
    trade_opening_id: int | None = None,
    position_id: str | None = None,
    include_rejected: bool = True,
) -> list[ExecutionOrder]:
    """Незакрытые стоп/тейк-строки с orderId по сделке, открытию и/или
    позиции биржи (через position_actions.position_id). Ни одного ключа —
    пустой список."""
    keys = []
    if trade_id is not None:
        keys.append(ExecutionOrder.trade_id == trade_id)
    if trade_opening_id is not None:
        keys.append(ExecutionOrder.trade_opening_id == trade_opening_id)
    if position_id:
        keys.append(ExecutionOrder.position_action_id.in_(
            select(PositionAction.id).where(
                PositionAction.user_id == user_id, PositionAction.position_id == position_id
            )
        ))
    if not keys:
        return []
    statuses = (*OPEN_STATUSES, OrderStatus.REJECTED) if include_rejected else OPEN_STATUSES
    rows = await session.scalars(
        select(ExecutionOrder).where(
            ExecutionOrder.user_id == user_id,
            ExecutionOrder.role.in_(CONDITIONAL_ROLES),
            ExecutionOrder.exchange_order_id.is_not(None),
            ExecutionOrder.status.in_(statuses),
            or_(*keys),
        ).order_by(ExecutionOrder.id)
    )
    return list(rows)


def history_facts(orders: Iterable[HistoryOrder]) -> dict[str, OrderStatus]:
    """orderId → FILLED/CANCELLED по истории (allOrders). Сработавший
    условник — FILLED и по дочернему ордеру (triggerOrderId), FILLED сильнее
    отмены."""
    facts: dict[str, OrderStatus] = {}
    for order in orders:
        if order.status in _FILLED:
            facts[order.order_id] = OrderStatus.FILLED
            if order.trigger_order_id:
                facts[order.trigger_order_id] = OrderStatus.FILLED
        elif order.status in _GONE:
            facts.setdefault(order.order_id, OrderStatus.CANCELLED)
    return facts


def settle(
    rows: Iterable[ExecutionOrder],
    *,
    open_order_ids: Collection[str],
    facts: Mapping[str, OrderStatus] | None = None,
    default: OrderStatus | None = OrderStatus.CANCELLED,
    exchange_ids: Collection[str] = (),
    include_rejected: bool = True,
    context: Mapping[str, object] | None = None,
) -> SettleResult:
    """Строки, чьих ордеров нет в open_order_ids: статус — из facts по
    orderId, иначе default (None — не решать, строка в undecided).
    cancel_source у CANCELLED — EXCHANGE, только если orderId в exchange_ids
    (доказано вызывающим), иначе NULL; у FILLED — NULL. Не коммитит."""
    result = SettleResult()
    for row in rows:
        if not settleable(row, include_rejected=include_rejected):
            continue
        assert row.exchange_order_id is not None
        if row.exchange_order_id in open_order_ids:
            result.standing.append(row.id)
            continue
        status = (facts or {}).get(row.exchange_order_id, default)
        if status is None:
            result.undecided.append(row.id)
            continue
        row.status = status
        row.cancel_source = (
            CancelSource.EXCHANGE
            if status is OrderStatus.CANCELLED and row.exchange_order_id in exchange_ids
            else None
        )
        result.settled.append(row.id)
    if result.settled:
        logger.info(
            "Условные ордера закрыты в базе",
            extra={**(context or {}), "ids": result.settled},
        )
    return result
