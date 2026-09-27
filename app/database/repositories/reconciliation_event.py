"""Репозиторий событий сверки (reconciler, шаг 15.6). См. ReconciliationEvent."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.reconciliation_event import ReconciliationEvent
from app.trading.enums import ReconciliationKind


class ReconciliationEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_open(self, user_id: int, dedup_key: str) -> ReconciliationEvent | None:
        """Нерешённое событие с этим ключом — есть, значит уже уведомлено."""
        stmt = select(ReconciliationEvent).where(
            ReconciliationEvent.user_id == user_id,
            ReconciliationEvent.dedup_key == dedup_key,
            ReconciliationEvent.resolved_at.is_(None),
        )
        return (await self.session.scalars(stmt)).first()

    def add(self, event: ReconciliationEvent) -> ReconciliationEvent:
        self.session.add(event)
        return event

    async def list_open(
        self, user_id: int, kinds: Iterable[ReconciliationKind]
    ) -> list[ReconciliationEvent]:
        stmt = select(ReconciliationEvent).where(
            ReconciliationEvent.user_id == user_id,
            ReconciliationEvent.kind.in_(list(kinds)),
            ReconciliationEvent.resolved_at.is_(None),
        )
        return list(await self.session.scalars(stmt))

    async def list_between(
        self, user_id: int, start: datetime, end: datetime
    ) -> list[ReconciliationEvent]:
        """Сырьё для сводки исполнения — окно по created_at."""
        stmt = select(ReconciliationEvent).where(
            ReconciliationEvent.user_id == user_id,
            ReconciliationEvent.created_at >= start,
            ReconciliationEvent.created_at < end,
        )
        return list(await self.session.scalars(stmt))

    async def flush(self) -> None:
        await self.session.flush()
