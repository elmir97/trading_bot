"""Журнал исходящих итоговых сообщений (M6, 09.10.2026, очередь A.2).

Одна строка — одно сообщение в чате (chat_id, message_id), которым бот
сообщил итог: открытие сделки (✅ / 🚨 / ALARM / ⏳ / отмена лимита),
действие с позицией, уведомление reconciler (закрыто по стопу/тейку,
расхождение). Сверка без скринов: что, кому и когда ушло, к какому
открытию / сделке / действию относится. Правка того же сообщения — та же
строка: прежний текст уходит в edits ([{at, text, kind}]), text — текущий.

Строку пишет app/bot/outbox.py после успешной отправки или правки; сбой
записи не мешает сообщению (ERROR в лог). Связи — SET NULL: запись о том,
что ушло в чат, переживает удаление сделки или открытия.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, IntPKMixin


class OutgoingMessage(IntPKMixin, Base):
    __tablename__ = "outgoing_messages"
    __table_args__ = (
        UniqueConstraint("chat_id", "message_id", name="uq_outgoing_messages_chat_message"),
        Index("ix_outgoing_messages_trade_opening_id", "trade_opening_id"),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # OPEN_<статус открытия> (OPEN_DONE, OPEN_ALARM, OPEN_WORKING, …),
    # OPEN_ALARM_REMINDER, ACTION_RESULT, RECON_<вид события сверки>
    # (RECON_CLOSED_STOP_LOSS, …). Без CHECK: новый вид — без миграции.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    trade_opening_id: Mapped[int | None] = mapped_column(
        ForeignKey("trade_openings.id", ondelete="SET NULL")
    )
    trade_id: Mapped[int | None] = mapped_column(ForeignKey("trades.id", ondelete="SET NULL"))
    position_action_id: Mapped[int | None] = mapped_column(
        ForeignKey("position_actions.id", ondelete="SET NULL")
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("now()"), nullable=False
    )
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    edits: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=sql_text("'[]'::jsonb"), nullable=False
    )

    def __repr__(self) -> str:
        return f"<OutgoingMessage {self.kind} {self.chat_id}/{self.message_id}>"


@dataclass(frozen=True, slots=True)
class OutgoingMeta:
    """К чему относится итоговое сообщение — то, что пишется рядом с ним."""

    user_id: int
    kind: str
    trade_opening_id: int | None = None
    trade_id: int | None = None
    position_action_id: int | None = None
