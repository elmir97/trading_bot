"""Репозиторий сделок.

Все выборки идут с обязательным user_id и с LIMIT. Загружать всю историю
в память ради показа последних десяти сделок недопустимо: на нескольких
тысячах записей это заметно, на десятках тысяч — неприемлемо.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models.trade import Trade, TradeFill
from app.database.models.user import User
from app.trading.enums import TradeStatus


class TradeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- Чтение ------------------------------------------------------------

    async def get(self, trade_id: int, user_id: int) -> Trade | None:
        stmt = (
            select(Trade)
            .where(Trade.id == trade_id, Trade.user_id == user_id)
            .options(
                selectinload(Trade.fills),
                selectinload(Trade.strategy),
                selectinload(Trade.mistakes),
            )
        )
        return await self.session.scalar(stmt)

    async def get_by_notification_id(self, notification_id: int) -> Trade | None:
        """Шаг 15.5.4: сделка, записанная по уведомлению (не больше одной —
        uq_trades_notification_id)."""
        trade: Trade | None = await self.session.scalar(
            select(Trade).where(Trade.notification_id == notification_id)
        )
        return trade

    async def list_open(self, user_id: int, limit: int = 50) -> list[Trade]:
        stmt = (
            select(Trade)
            .where(Trade.user_id == user_id, Trade.status == TradeStatus.OPEN)
            .options(selectinload(Trade.strategy))
            .order_by(Trade.opened_at.desc())
            .limit(limit)
        )
        return list(await self.session.scalars(stmt))

    async def list_recent(
        self, user_id: int, limit: int = 10, offset: int = 0
    ) -> list[Trade]:
        stmt = (
            select(Trade)
            .where(Trade.user_id == user_id)
            .options(selectinload(Trade.strategy))
            .order_by(Trade.opened_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(await self.session.scalars(stmt))

    async def list_all_open(self) -> list[Trade]:
        """Все открытые позиции всех пользователей — для фонового монитора.

        В отличие от list_open, без фильтра по user_id: монитор TP/SL
        (этап 12) обходит биржу сразу для всех, а не по одному пользователю,
        чтобы не дублировать запросы цен на каждого. Настройки пользователя
        грузятся тем же запросом — монитор проверяет notification_enabled
        на каждой сделке, без них это был бы N+1.
        """
        stmt = (
            select(Trade)
            .where(Trade.status == TradeStatus.OPEN)
            .options(selectinload(Trade.user).selectinload(User.settings))
        )
        return list(await self.session.scalars(stmt))

    async def list_closed_between(
        self, user_id: int, start: datetime, end: datetime
    ) -> list[Trade]:
        """Закрытые сделки за период — основа всех отчётов.

        Фильтр по closed_at, а не opened_at: сделка, открытая вчера и
        закрытая сегодня, относится к сегодняшнему результату.
        """
        stmt = (
            select(Trade)
            .where(
                Trade.user_id == user_id,
                Trade.status == TradeStatus.CLOSED,
                Trade.closed_at >= start,
                Trade.closed_at < end,
            )
            .options(selectinload(Trade.strategy))
            .order_by(Trade.closed_at)
        )
        return list(await self.session.scalars(stmt))

    async def list_unannotated(self, user_id: int, limit: int = 20) -> list[Trade]:
        """Импортированные сделки, ждущие разметки."""
        stmt = (
            select(Trade)
            .where(
                Trade.user_id == user_id,
                Trade.is_annotated.is_(False),
                Trade.status == TradeStatus.CLOSED,
            )
            .order_by(Trade.closed_at.desc())
            .limit(limit)
        )
        return list(await self.session.scalars(stmt))

    async def count_opened_between(
        self, user_id: int, start: datetime, end: datetime
    ) -> int:
        """Для проверки лимита сделок в день."""
        stmt = (
            select(func.count())
            .select_from(Trade)
            .where(
                Trade.user_id == user_id,
                Trade.opened_at >= start,
                Trade.opened_at < end,
                Trade.status != TradeStatus.CANCELLED,
            )
        )
        return await self.session.scalar(stmt) or 0

    async def sum_pnl_between(
        self, user_id: int, start: datetime, end: datetime
    ) -> float | None:
        """Сумма PnL за период. Возвращает None, если сделок нет."""
        from decimal import Decimal

        stmt = select(func.sum(Trade.pnl)).where(
            Trade.user_id == user_id,
            Trade.status == TradeStatus.CLOSED,
            Trade.closed_at >= start,
            Trade.closed_at < end,
        )
        result: Decimal | None = await self.session.scalar(stmt)
        return result  # type: ignore[return-value]

    async def find_by_external_position(
        self, user_id: int, exchange: str, external_position_id: str
    ) -> Trade | None:
        """Для дедупликации при импорте."""
        stmt = (
            select(Trade)
            .where(
                Trade.user_id == user_id,
                Trade.exchange == exchange,
                Trade.external_position_id == external_position_id,
            )
            .options(selectinload(Trade.fills))
        )
        return await self.session.scalar(stmt)

    async def existing_fill_ids(
        self, user_id: int, exchange: str, external_ids: list[str]
    ) -> set[str]:
        """Какие из переданных fill уже импортированы.

        Один запрос вместо проверки по одному: импорт за год может
        принести тысячи исполнений.
        """
        if not external_ids:
            return set()
        stmt = select(TradeFill.external_fill_id).where(
            TradeFill.user_id == user_id,
            TradeFill.exchange == exchange,
            TradeFill.external_fill_id.in_(external_ids),
        )
        return {row for row in await self.session.scalars(stmt) if row}

    # --- Запись ------------------------------------------------------------

    def add(self, trade: Trade) -> Trade:
        self.session.add(trade)
        return trade

    def add_fill(self, fill: TradeFill) -> TradeFill:
        self.session.add(fill)
        return fill

    async def flush(self) -> None:
        await self.session.flush()

    async def delete(self, trade: Trade) -> None:
        await self.session.delete(trade)
