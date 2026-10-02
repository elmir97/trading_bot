"""Отметка «уведомили о приближении к уровню» (этап 5) — дедуп по уровню.

Ключ — (пользователь, символ, сторона, SL или TP, цена уровня). Перенос
стопа или тейка (ботом или руками на бирже) даёт новый ключ — уведомление
для нового уровня не заблокировано. Строка ставится только после
окончательного исхода доставки и снимается, когда цена ушла назад за
порог минус гистерезис (app/workers/positions.py), когда уровень сменился
или позиция исчезла.

Источник — позиции биржи, а не сделки журнала: старые отметки
trades.tp/sl_approach_notified_at больше не пишутся (удалятся вместе с
таблицами сигналов отдельной миграцией).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, IntPKMixin, PriceNumeric
from app.trading.enums import TradeSide


class PositionAlert(IntPKMixin, Base):
    __tablename__ = "position_alerts"
    __table_args__ = (
        CheckConstraint("kind IN ('SL', 'TP')", name="kind_known"),
        UniqueConstraint(
            "user_id", "symbol", "side", "kind", "level_price", name="uq_position_alerts_key"
        ),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[TradeSide] = mapped_column(
        Enum(TradeSide, native_enum=False, length=8), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(2), nullable=False)   # 'SL' | 'TP'
    level_price: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False)
    notified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<PositionAlert {self.symbol} {self.side} {self.kind} {self.level_price}>"
