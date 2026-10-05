"""Репозиторий ордеров исполнения (этап 15).

Только персистентность. Идемпотентность (запись PENDING до HTTP-запроса,
разрешение UNKNOWN через client_order_id) — логика вызывающего кода, не
этого слоя.
"""

from __future__ import annotations

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

    async def conditionals_for_notification(
        self, user_id: int, notification_id: int
    ) -> list[ExecutionOrder]:
        """Шаг 15.6: строки стопа и тейка входа — orderId условников биржи,
        по которым reconciler узнаёт выход по стопу/тейку (triggerOrderId)."""
        stmt = select(ExecutionOrder).where(
            ExecutionOrder.user_id == user_id,
            ExecutionOrder.notification_id == notification_id,
            ExecutionOrder.role.in_((OrderRole.STOP_LOSS, OrderRole.TAKE_PROFIT)),
        )
        return list(await self.session.scalars(stmt))

    async def entry_for_trade(self, user_id: int, trade_id: int) -> ExecutionOrder | None:
        """28.09: ENTRY-строка входа сделки — её risk_amount задаёт 1R для
        сверки PnL с биржей (PNL_MISMATCH)."""
        stmt = (
            select(ExecutionOrder)
            .where(
                ExecutionOrder.user_id == user_id,
                ExecutionOrder.trade_id == trade_id,
                ExecutionOrder.role == OrderRole.ENTRY,
            )
            .order_by(ExecutionOrder.id.desc())
            .limit(1)
        )
        return (await self.session.scalars(stmt)).first()

    async def list_unresolved_entries(self, user_id: int) -> list[ExecutionOrder]:
        """Шаг 15.6: входы с неизвестным исходом — UNKNOWN, PENDING (процесс
        упал между коммитом и ответом биржи) и SUBMITTED без подтверждения.
        Окно (10 минут) проверяет reconciler: ему нужен и created_at."""
        stmt = select(ExecutionOrder).where(
            ExecutionOrder.user_id == user_id,
            ExecutionOrder.role == OrderRole.ENTRY,
            ExecutionOrder.client_order_id.is_not(None),
            ExecutionOrder.status.in_(
                (OrderStatus.UNKNOWN, OrderStatus.PENDING, OrderStatus.SUBMITTED)
            ),
            # 05.10.2026: входы открытия из бота разбирает своё восстановление
            # (app/execution/opening/recovery), без 10-минутного окна.
            ExecutionOrder.trade_opening_id.is_(None),
        )
        return list(await self.session.scalars(stmt))

    async def conditionals_for_trade(self, user_id: int, trade_id: int) -> list[ExecutionOrder]:
        """05.10.2026: стоп и тейк сделки, открытой из бота (TradeSource.BOT), —
        orderId условников, по которым reconciler узнаёт выход по стопу/тейку.
        Последние по id — запасной стоп после вложенного."""
        stmt = (
            select(ExecutionOrder)
            .where(
                ExecutionOrder.user_id == user_id,
                ExecutionOrder.trade_id == trade_id,
                ExecutionOrder.role.in_((OrderRole.STOP_LOSS, OrderRole.TAKE_PROFIT)),
                ExecutionOrder.exchange_order_id.is_not(None),
            )
            .order_by(ExecutionOrder.id.desc())
        )
        return list(await self.session.scalars(stmt))

    async def users_with_real_entries(self) -> list[int]:
        """Пользователи, у которых был хоть один реальный вход (не сухой
        прогон и не наблюдение) — только их счета сверяет reconciler."""
        stmt = (
            select(ExecutionOrder.user_id)
            .where(
                ExecutionOrder.role == OrderRole.ENTRY,
                ExecutionOrder.exchange_order_id.is_not(None)
                | ExecutionOrder.status.in_(
                    (OrderStatus.UNKNOWN, OrderStatus.PENDING, OrderStatus.SUBMITTED)
                ),
            )
            .distinct()
        )
        return list(await self.session.scalars(stmt))

    async def list_by_signal(self, user_id: int, signal_id: int) -> list[ExecutionOrder]:
        stmt = select(ExecutionOrder).where(
            ExecutionOrder.user_id == user_id,
            ExecutionOrder.signal_id == signal_id,
        )
        return list(await self.session.scalars(stmt))

    async def exchange_order_ids(self, user_id: int) -> set[str]:
        """Шаг 15.5.4: все orderId биржи, записанные за ордерами бота (вход,
        стоп, тейк) — импорт истории пропускает их исполнения: сделка бота
        уже в журнале, повторный импорт задвоил бы её."""
        stmt = select(ExecutionOrder.exchange_order_id).where(
            ExecutionOrder.user_id == user_id,
            ExecutionOrder.exchange_order_id.is_not(None),
        )
        return {value for value in await self.session.scalars(stmt) if value}

    def add(self, order: ExecutionOrder) -> ExecutionOrder:
        self.session.add(order)
        return order

    async def flush(self) -> None:
        await self.session.flush()
