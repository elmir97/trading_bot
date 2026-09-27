"""События сверки журнала с биржей (reconciler, шаг 15.6).

Одна строка — одна находка reconciler: закрытие сделки фактом биржи,
разрешённый UNKNOWN/PENDING вход, или расхождение без однозначного факта.
Нужна для двух вещей: дедуп уведомлений (открытое расхождение с тем же
dedup_key не уведомляет каждую минуту — частичный UNIQUE по строкам без
resolved_at) и «Аномалии» ежедневной сводки (окно по created_at).

resolved_at — расхождение ушло (позиция закрылась, стоп появился): строка
остаётся историей, а повторное появление того же расхождения даёт новую
строку и новое уведомление.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, IntPKMixin
from app.trading.enums import ReconciliationKind


class ReconciliationEvent(IntPKMixin, Base):
    __tablename__ = "reconciliation_events"
    __table_args__ = (
        Index(
            "uq_reconciliation_events_open",
            "user_id",
            "dedup_key",
            unique=True,
            postgresql_where=text("resolved_at IS NULL"),
        ),
        Index("ix_reconciliation_events_user_created", "user_id", "created_at"),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # SET NULL: событие — история, переживает удаление сделки/строки ордера.
    trade_id: Mapped[int | None] = mapped_column(
        ForeignKey("trades.id", ondelete="SET NULL")
    )
    execution_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("execution_orders.id", ondelete="SET NULL")
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[ReconciliationKind] = mapped_column(
        Enum(ReconciliationKind, native_enum=False, length=32), nullable=False
    )
    # Что именно найдено — стабильная строка вида
    # "stop_missing:trade:4" / "orphan:SOL-USDT:LONG:<positionId>": одно и то
    # же расхождение на следующем цикле даёт тот же ключ.
    dedup_key: Mapped[str] = mapped_column(String(128), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<ReconciliationEvent {self.kind} {self.dedup_key}>"
