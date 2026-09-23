"""Обнаруженные сканером сетапы (этап 12).

Одна строка — один "слот" (user, symbol, timeframe, level). Новый скан не
создаёт новую строку каждый раз, а обновляет существующий слот — так
дедуп уведомлений сводится к сравнению fingerprint и expires_at одной
строки, без поиска по истории.

updated_at (из TimestampMixin, onupdate=func.now()) и есть "последний раз
сетап подтверждён сканом" — отдельная колонка last_seen_at не нужна.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, IntPKMixin, PriceNumeric, TimestampMixin
from app.trading.enums import SignalDirection, SignalLevel, SignalRecordStatus

if TYPE_CHECKING:
    from app.database.models.user import User


class SignalRecord(IntPKMixin, TimestampMixin, Base):
    __tablename__ = "signals"
    __table_args__ = (
        # Один активный слот на (пользователь, инструмент, таймфрейм,
        # уровень готовности) — READY и FORMING по одному символу живут
        # как две независимые строки, а не конкурируют за одну.
        UniqueConstraint(
            "user_id", "symbol", "timeframe", "level", name="uq_signal_slot"
        ),
        Index("ix_signals_user_status", "user_id", "status"),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    level: Mapped[SignalLevel] = mapped_column(
        Enum(SignalLevel, native_enum=False, length=8), nullable=False
    )
    status: Mapped[SignalRecordStatus] = mapped_column(
        Enum(SignalRecordStatus, native_enum=False, length=8),
        default=SignalRecordStatus.ACTIVE,
        server_default="ACTIVE",
        nullable=False,
    )

    setup: Mapped[str] = mapped_column(String(64), nullable=False)
    # Направление известно только у READY: FORMING получается из wait_signal,
    # где направление ещё не зафиксировано в объекте Signal.
    direction: Mapped[SignalDirection | None] = mapped_column(
        Enum(SignalDirection, native_enum=False, length=8)
    )
    # sha256 от условий сетапа — меняется, когда меняются цены входа/стопа/
    # цели (READY) или сам сетап-кандидат (FORMING). Дедуп сравнивает его,
    # а не содержимое целиком.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    entry_low: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    entry_high: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    stop_loss: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    take_profit: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    confidence: Mapped[int | None] = mapped_column(SmallInteger)

    # Готовый текст уведомления — рендерится один раз при обнаружении,
    # чтобы формат сообщения не зависел от того, что доживёт до отправки.
    detail: Mapped[str] = mapped_column(Text, nullable=False)

    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # "Время жизни" слота: после этого момента тот же fingerprint уже не
    # считается недавно уведомлённым — повторный скан пришлёт уведомление
    # заново, даже если сетап буквально не изменился.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # УСТАРЕЛО (шаг 15.5.2а): код это поле больше не читает и не пишет —
    # «по этому уже открывали сделку» теперь SignalNotification.trade_opened_at
    # (слот переиспользуется, и отметка на нём навсегда сжигала слот).
    # Колонка удаляется позже отдельной миграцией; downgrade 15.5.2а
    # переносит сюда отметки из signal_notifications.
    trade_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship()

    def __repr__(self) -> str:
        return (
            f"<SignalRecord {self.symbol} {self.timeframe} "
            f"{self.level} {self.status}>"
        )
