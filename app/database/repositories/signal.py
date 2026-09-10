"""Репозиторий обнаруженных сканером сетапов (этап 12).

Держит только выборку и запись одного "слота" (user, symbol, timeframe,
level) — сравнение fingerprint/expires_at и решение, слать ли уведомление,
это бизнес-логика сканера (app/workers/scanner.py), а не персистентности.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.signal import SignalRecord
from app.trading.enums import SignalLevel, SignalRecordStatus


class SignalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_active_slot(
        self, user_id: int, symbol: str, timeframe: str, level: SignalLevel
    ) -> SignalRecord | None:
        stmt = select(SignalRecord).where(
            SignalRecord.user_id == user_id,
            SignalRecord.symbol == symbol,
            SignalRecord.timeframe == timeframe,
            SignalRecord.level == level,
            SignalRecord.status == SignalRecordStatus.ACTIVE,
        )
        return await self.session.scalar(stmt)

    async def get_slot(
        self, user_id: int, symbol: str, timeframe: str, level: SignalLevel
    ) -> SignalRecord | None:
        """Строка слота в любом статусе.

        uq_signal_slot не включает status, так что EXPIRED-строка всё ещё
        занимает (user, symbol, timeframe, level) — новый INSERT для того же
        слота словит IntegrityError. Вызывающий код должен переиспользовать
        (UPDATE) эту строку, а не создавать новую, когда сетап появляется
        снова после того как погас.
        """
        stmt = select(SignalRecord).where(
            SignalRecord.user_id == user_id,
            SignalRecord.symbol == symbol,
            SignalRecord.timeframe == timeframe,
            SignalRecord.level == level,
        )
        return await self.session.scalar(stmt)

    async def expire_active_slot(
        self, user_id: int, symbol: str, timeframe: str, level: SignalLevel
    ) -> None:
        """Гасит активный слот, если сетап на этом уровне больше не находится.

        Не ждёт TTL: если скан больше не подтверждает FORMING/READY, слот
        считается закрытым немедленно, а не зависает в ACTIVE до истечения.
        """
        record = await self.get_active_slot(user_id, symbol, timeframe, level)
        if record is not None:
            record.status = SignalRecordStatus.EXPIRED

    def add(self, record: SignalRecord) -> SignalRecord:
        self.session.add(record)
        return record

    async def flush(self) -> None:
        await self.session.flush()
