"""Репозиторий ордеров исполнения (этап 15).

Только персистентность. Идемпотентность (запись PENDING до HTTP-запроса,
разрешение UNKNOWN через client_order_id) — логика app/execution/service.py
(этап 15.5), не этого слоя.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.execution_order import ExecutionOrder


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

    async def list_by_signal(self, user_id: int, signal_id: int) -> list[ExecutionOrder]:
        stmt = select(ExecutionOrder).where(
            ExecutionOrder.user_id == user_id,
            ExecutionOrder.signal_id == signal_id,
        )
        return list(await self.session.scalars(stmt))

    def add(self, order: ExecutionOrder) -> ExecutionOrder:
        self.session.add(order)
        return order

    async def flush(self) -> None:
        await self.session.flush()
