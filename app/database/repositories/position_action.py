"""Репозиторий действий с позицией (этап 4) — только то, что нужно импорту
«📥 В журнал» (К3, 27.10): связать строки действий позиции, сделанные до
импорта, с новой сделкой."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.position_action import PositionAction
from app.trading.enums import ExchangeKeyMode, TradeSide


class PositionActionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def link_to_trade(
        self, user_id: int, trade_id: int, *, symbol: str, side: TradeSide,
        account_mode: ExchangeKeyMode, position_id: str, since: datetime,
    ) -> list[PositionAction]:
        """Строки действий этой позиции без сделки → trade_id. Позиция — по
        positionId биржи (без него не связываем: можно задеть чужую позицию
        того же символа) и не раньше её открытия (since). Возвращает
        связанные строки от ранней к поздней."""
        rows = list(await self.session.scalars(
            select(PositionAction).where(
                PositionAction.user_id == user_id,
                PositionAction.trade_id.is_(None),
                PositionAction.symbol == symbol,
                PositionAction.side == side,
                PositionAction.account_mode == account_mode,
                PositionAction.position_id == position_id,
                PositionAction.created_at >= since,
            ).order_by(PositionAction.created_at, PositionAction.id).with_for_update()
        ))
        for row in rows:
            row.trade_id = trade_id
        return rows
