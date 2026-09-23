"""Репозиторий отправленных уведомлений о сетапах (шаг 15.5.2а).

См. docstring SignalNotification: строка — неизменяемый снимок одного
отправленного сообщения, адресуемый notification_id. Решение, слать ли
уведомление, остаётся бизнес-логикой сканера (app/workers/scanner.py).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.signal_notification import SignalNotification
from app.trading.enums import SignalLevel


class SignalNotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, notification: SignalNotification) -> SignalNotification:
        self.session.add(notification)
        return notification

    async def flush(self) -> None:
        await self.session.flush()

    async def get(self, notification_id: int, user_id: int) -> SignalNotification | None:
        """Чужое уведомление — None, как и несуществующее: callback_data
        приходит от клиента и не доказывает, что id принадлежит нажавшему."""
        stmt = select(SignalNotification).where(
            SignalNotification.id == notification_id,
            SignalNotification.user_id == user_id,
        )
        notification: SignalNotification | None = await self.session.scalar(stmt)
        return notification

    async def latest_for_slot(self, signal_id: int) -> SignalNotification | None:
        """Последнее уведомление слота — сканер сверяет его expires_at, чтобы
        заново уведомить о READY-сетапе, который держится дольше TTL (Р1)."""
        stmt = (
            select(SignalNotification)
            .where(SignalNotification.signal_id == signal_id)
            .order_by(SignalNotification.notified_at.desc(), SignalNotification.id.desc())
            .limit(1)
        )
        latest: SignalNotification | None = await self.session.scalar(stmt)
        return latest

    async def exists_traded(self, signal_id: int, fingerprint: str) -> bool:
        """Гвард SETUP_ALREADY_TRADED: по этому же сетапу (слот + fingerprint)
        уже открывали сделку через любое уведомление, не обязательно текущее —
        повторный вход в тот же сетап запрещён (наращивание риска)."""
        stmt = select(
            select(SignalNotification.id)
            .where(
                SignalNotification.signal_id == signal_id,
                SignalNotification.fingerprint == fingerprint,
                SignalNotification.trade_opened_at.is_not(None),
            )
            .exists()
        )
        return bool(await self.session.scalar(stmt))

    async def count_ready_between(
        self, user_id: int, start: datetime, end: datetime
    ) -> int:
        """Раздел 12а ТЗ: «Сигналов READY» в сводке исполнения — число
        отправленных READY-уведомлений (событий) в окне, а не число слотов:
        слот, уведомивший дважды за сутки, считается дважды."""
        stmt = select(func.count()).select_from(SignalNotification).where(
            SignalNotification.user_id == user_id,
            SignalNotification.level == SignalLevel.READY,
            SignalNotification.notified_at >= start,
            SignalNotification.notified_at < end,
        )
        return (await self.session.scalar(stmt)) or 0
