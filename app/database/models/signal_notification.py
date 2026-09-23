"""Отправленные уведомления о сетапах — неизменяемый снимок (шаг 15.5.2а).

Строка слота (SignalRecord) изменчива: сканер перезаписывает уровни,
направление, fingerprint и expires_at на каждом проходе. Кнопка входа,
карточка подтверждения и «Да» обязаны опираться на то, что пользователь
видел в конкретном сообщении, а не на то, во что слот превратился к
моменту нажатия. Поэтому каждое реально отправленное уведомление (READY и
FORMING) пишет сюда свою строку, и дальше путь исполнения адресуется
notification_id, а не signal_id.

После вставки строка не меняется, кроме trade_opened_at. TimestampMixin
намеренно не подключён: updated_at у неизменяемой строки ничего не значит,
а момент создания и есть notified_at.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Index, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, IntPKMixin, PriceNumeric
from app.trading.enums import SignalDirection, SignalLevel

if TYPE_CHECKING:
    from app.database.models.signal import SignalRecord


class SignalNotification(IntPKMixin, Base):
    __tablename__ = "signal_notifications"
    __table_args__ = (
        Index("ix_signal_notifications_signal_id_notified_at", "signal_id", "notified_at"),
        Index("ix_signal_notifications_user_id_notified_at", "user_id", "notified_at"),
    )

    # CASCADE: снимок без слота бессмыслен — это уведомление именно об этом
    # (user, symbol, timeframe, level).
    signal_id: Mapped[int] = mapped_column(
        ForeignKey("signals.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    level: Mapped[SignalLevel] = mapped_column(
        Enum(SignalLevel, native_enum=False, length=8), nullable=False
    )
    # Тот же sha256, что был у слота в момент отправки. Сравнение с текущим
    # fingerprint слота — и есть проверка «сетап не обновился после
    # уведомления» (SIGNAL_SUPERSEDED).
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    setup: Mapped[str] = mapped_column(String(64), nullable=False)
    direction: Mapped[SignalDirection | None] = mapped_column(
        Enum(SignalDirection, native_enum=False, length=8)
    )
    entry_low: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    entry_high: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    stop_loss: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    take_profit: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    confidence: Mapped[int | None] = mapped_column(SmallInteger)

    notified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # notified_at + TTL сигнала (Settings.setup_scanner_ttl_hours), фиксируется
    # при вставке. В отличие от SignalRecord.expires_at пересканы его НЕ
    # продлевают — это срок жизни кнопки из конкретного сообщения.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # По этому уведомлению уже открывали сделку (гвард SIGNAL_ALREADY_USED).
    # Единственное поле, которое меняется после вставки.
    trade_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    signal: Mapped[SignalRecord] = relationship()

    @classmethod
    def snapshot_of(
        cls, slot: SignalRecord, *, notified_at: datetime, expires_at: datetime
    ) -> SignalNotification:
        """Снимок текущего состояния слота — сканер зовёт это ровно в момент
        отправки уведомления, после того как записал в слот свежие уровни."""
        return cls(
            signal_id=slot.id,
            user_id=slot.user_id,
            level=slot.level,
            fingerprint=slot.fingerprint,
            setup=slot.setup,
            direction=slot.direction,
            entry_low=slot.entry_low,
            entry_high=slot.entry_high,
            stop_loss=slot.stop_loss,
            take_profit=slot.take_profit,
            confidence=slot.confidence,
            notified_at=notified_at,
            expires_at=expires_at,
        )

    def __repr__(self) -> str:
        return (
            f"<SignalNotification id={self.id} signal_id={self.signal_id} "
            f"{self.level} notified_at={self.notified_at}>"
        )
