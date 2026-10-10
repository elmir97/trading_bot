"""Журнал нажатий кнопок исполнения (exn:open / exn:yes / exn:no; с миграции
M1 — и кнопки действий с позицией pm_*, этап 4).

Префлайт 15.7: на реальном счёте «кто нажал» подтверждается записью, а не
выводом из кода. Строка пишется первым действием хендлера, из отдельной
сессии с немедленным коммитом (app/execution/callback_audit.py): транзакция
апдейта откатывается при падении хендлера — ровно тогда, когда запись нужнее
всего. Исход нажатия — в execution_orders, здесь только сам факт.

notification_id без FK и nullable: в кнопке может прийти id чужого,
несуществующего уведомления или не число (битые данные) — запись не должна
падать. telegram_id не храним: пользователя разрешает middleware из
event_from_user, user_id и есть нажавший, его telegram_id — в users.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, IntPKMixin


class ExecutionCallback(IntPKMixin, Base):
    __tablename__ = "execution_callbacks"
    __table_args__ = (
        CheckConstraint(
            "action IN ('open', 'yes', 'no', 'pm_open', 'pm_yes', 'pm_yes_risk', 'pm_no', "
            "'to_yes', 'to_yes_warn', 'to_no', 'to_cancel', 'ma_confirm', 'ma_cancel', "
            "'to_close', 'to_close_yes', 'to_close_no')",
            name="action_known",
        ),
        Index("ix_execution_callbacks_user_created", "user_id", "created_at"),
        Index("ix_execution_callbacks_notification", "notification_id"),
        Index("ix_execution_callbacks_position_action", "position_action_id"),
        Index("ix_execution_callbacks_trade_opening", "trade_opening_id"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # ExecutionCallbackAction.value
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    notification_id: Mapped[int | None] = mapped_column()
    # Этап 4: кнопки действий с позицией (pm_*) адресуют position_actions.id.
    # Без FK и nullable — по той же причине, что notification_id.
    position_action_id: Mapped[int | None] = mapped_column()
    # M5: кнопки и вызовы открытия сделки (to_*, ma_*) — trade_openings.id.
    trade_opening_id: Mapped[int | None] = mapped_column()
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    callback_query_id: Mapped[str | None] = mapped_column(String(64))
    # Этап 5 (M2): callback_data кнопки как есть (лимит Telegram — 64 байта).
    # Нажатие pm_open пишется до карточки (position_action_id NULL) — по коду
    # кнопки (pa:sl:XRP-USDT:L) его связывают с карточкой без лога.
    raw_data: Mapped[str | None] = mapped_column(String(64))

    def __repr__(self) -> str:
        return f"<ExecutionCallback {self.action} n={self.notification_id}>"
