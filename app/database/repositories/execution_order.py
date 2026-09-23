"""Репозиторий ордеров исполнения (этап 15).

Только персистентность. Идемпотентность (запись PENDING до HTTP-запроса,
разрешение UNKNOWN через client_order_id) — логика app/execution/service.py
(этап 15.5), не этого слоя.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.execution_order import ExecutionOrder
from app.trading.enums import OrderRole, OrderStatus


class ExecutionOrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, order_id: int, user_id: int) -> ExecutionOrder | None:
        stmt = select(ExecutionOrder).where(
            ExecutionOrder.id == order_id, ExecutionOrder.user_id == user_id
        )
        return await self.session.scalar(stmt)

    async def get_by_client_order_id(
        self, user_id: int, client_order_id: str
    ) -> ExecutionOrder | None:
        """Ключ идемпотентности: перед отправкой на биржу код обязан
        проверить, что запись с этим client_order_id ещё не существует."""
        stmt = select(ExecutionOrder).where(
            ExecutionOrder.user_id == user_id,
            ExecutionOrder.client_order_id == client_order_id,
        )
        return await self.session.scalar(stmt)

    async def claimed_conditional_order_ids(
        self, user_id: int, *, exclude_notification_id: int
    ) -> set[str]:
        """Шаг 15.5.3: orderId биржи, уже записанные за стопами/тейками
        ДРУГИХ входов — find_our_conditional не отдаст один и тот же
        условный ордер двум входам по одному символу."""
        stmt = select(ExecutionOrder.exchange_order_id).where(
            ExecutionOrder.user_id == user_id,
            ExecutionOrder.role.in_((OrderRole.STOP_LOSS, OrderRole.TAKE_PROFIT)),
            ExecutionOrder.exchange_order_id.is_not(None),
            ExecutionOrder.notification_id.is_distinct_from(exclude_notification_id),
        )
        return {value for value in await self.session.scalars(stmt) if value}

    async def list_by_signal(self, user_id: int, signal_id: int) -> list[ExecutionOrder]:
        stmt = select(ExecutionOrder).where(
            ExecutionOrder.user_id == user_id,
            ExecutionOrder.signal_id == signal_id,
        )
        return list(await self.session.scalars(stmt))

    async def list_entries_between(
        self, user_id: int, start: datetime, end: datetime
    ) -> list[ExecutionOrder]:
        """Раздел 12а ТЗ: сырьё для дневной сводки исполнения.

        role=ENTRY, а не все строки — один подтверждённый вход пишет три
        строки (вход, стоп, тейк, раздел 8 ТЗ), а REFUSED/DECLINED/EXPIRED
        (раздел 12а) всегда одна строка с role=ENTRY. Без фильтра по роли
        подтверждённые входы утроились бы в подсчётах сводки.
        """
        stmt = select(ExecutionOrder).where(
            ExecutionOrder.user_id == user_id,
            ExecutionOrder.role == OrderRole.ENTRY,
            ExecutionOrder.created_at >= start,
            ExecutionOrder.created_at < end,
        )
        return list(await self.session.scalars(stmt))

    async def list_unprotected_between(
        self, user_id: int, start: datetime, end: datetime
    ) -> list[ExecutionOrder]:
        """Шаг 15.5.3: строки STOP_LOSS, у которых стоп на бирже не
        подтверждён — спасение отклонено/исход неизвестен (REJECTED/UNKNOWN),
        застряло в PENDING, или openOrders не прочитан (ERROR,
        STOP_UNVERIFIED). Сырьё для аномалии «позиция без стопа» в сводке."""
        stmt = select(ExecutionOrder).where(
            ExecutionOrder.user_id == user_id,
            ExecutionOrder.role == OrderRole.STOP_LOSS,
            ExecutionOrder.status.in_((
                OrderStatus.REJECTED, OrderStatus.UNKNOWN,
                OrderStatus.PENDING, OrderStatus.ERROR,
            )),
            ExecutionOrder.created_at >= start,
            ExecutionOrder.created_at < end,
        )
        return list(await self.session.scalars(stmt))

    def add(self, order: ExecutionOrder) -> ExecutionOrder:
        self.session.add(order)
        return order

    async def flush(self) -> None:
        await self.session.flush()
