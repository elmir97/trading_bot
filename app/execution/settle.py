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
историей решения (решение владельца 10.10)."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.database.models.execution_order import ExecutionOrder
from app.database.models.position_action import PositionAction
from app.exchanges.base import HistoryOrder
from app.trading.enums import CancelSource, OrderRole, OrderStatus

logger = get_logger(__name__)

CONDITIONAL_ROLES = (OrderRole.STOP_LOSS, OrderRole.TAKE_PROFIT)
OPEN_STATUSES = (OrderStatus.SUBMITTED, OrderStatus.UNKNOWN)
_FILLED = frozenset({"FILLED"})
_GONE = frozenset({"CANCELLED", "CANCELED", "EXPIRED"})


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
    source: CancelSource | None = CancelSource.EXCHANGE,
    include_rejected: bool = True,
    context: Mapping[str, object] | None = None,
) -> SettleResult:
    """Строки, чьих ордеров нет в open_order_ids: статус — из facts по
    orderId, иначе default (None — не решать, строка в undecided).
    cancel_source — source у CANCELLED, у FILLED — NULL. Не коммитит."""
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
        row.cancel_source = source if status is OrderStatus.CANCELLED else None
        result.settled.append(row.id)
    if result.settled:
        logger.info(
            "Условные ордера закрыты в базе",
            extra={**(context or {}), "ids": result.settled},
        )
    return result
