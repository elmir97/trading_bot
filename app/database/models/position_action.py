"""Действие с открытой позицией на бирже (этапы 3–4) — снимок карточки.

Одна строка — одна показанная карточка подтверждения: что пользователь
увидел (уровни, объём, риск до и после, комиссия) и чем всё кончилось. Её
id порождает идемпотентный client_order_id ордеров действия (как раньше
notification_id у входа): повторное «Да» или «Да» после рестарта адресует ту
же строку и не может отправить второй ордер (UNIQUE на
execution_orders.client_order_id).

Позиция — с биржи (symbol, side, position_id), сделка журнала — если
связана (trade_id, SET NULL: действие переживает удаление сделки). Все
числа снимка nullable: часть действий их не имеет (у закрытия нет нового
уровня, у переноса стопа — объёма закрытия).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import (
    Base,
    IntPKMixin,
    MoneyNumeric,
    PriceNumeric,
    QuantityNumeric,
    RatioNumeric,
    TimestampMixin,
)
from app.trading.enums import PositionActionKind, PositionActionStatus, TradeSide


class PositionAction(IntPKMixin, TimestampMixin, Base):
    __tablename__ = "position_actions"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('MOVE_STOP', 'SET_TAKE', 'CLOSE_PARTIAL', 'CLOSE_FULL')",
            name="kind_known",
        ),
        CheckConstraint(
            "status IN ('CARD', 'DECLINED', 'EXPIRED', 'REFUSED', 'DRY_RUN', "
            "'SUBMITTED', 'DONE', 'FAILED')",
            name="status_known",
        ),
        Index("ix_position_actions_user_created", "user_id", "created_at"),
        Index("ix_position_actions_user_symbol_side", "user_id", "symbol", "side"),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    trade_id: Mapped[int | None] = mapped_column(
        ForeignKey("trades.id", ondelete="SET NULL")
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[TradeSide] = mapped_column(
        Enum(TradeSide, native_enum=False, length=8), nullable=False
    )
    # positionId биржи строкой (значения > 2^53 приходят числом — не float).
    position_id: Mapped[str | None] = mapped_column(String(64))

    kind: Mapped[PositionActionKind] = mapped_column(
        Enum(PositionActionKind, native_enum=False, length=16, create_constraint=False),
        nullable=False,
    )
    status: Mapped[PositionActionStatus] = mapped_column(
        Enum(PositionActionStatus, native_enum=False, length=16, create_constraint=False),
        default=PositionActionStatus.CARD,
        server_default="CARD",
        nullable=False,
    )
    # Параметры действия: {"level": "1.5"}, {"fraction": "0.25"},
    # {"breakeven": true} — строки Decimal, не float.
    params: Mapped[dict[str, object] | None] = mapped_column(JSONB)

    # --- снимок карточки ----------------------------------------------------
    mark_price: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    entry_price: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    position_qty: Mapped[Decimal | None] = mapped_column(QuantityNumeric)
    close_qty: Mapped[Decimal | None] = mapped_column(QuantityNumeric)
    current_stop: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    current_take: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    new_level: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    risk_before: Mapped[Decimal | None] = mapped_column(MoneyNumeric)
    risk_after: Mapped[Decimal | None] = mapped_column(MoneyNumeric)
    risk_before_r: Mapped[Decimal | None] = mapped_column(RatioNumeric)
    risk_after_r: Mapped[Decimal | None] = mapped_column(RatioNumeric)
    fee: Mapped[Decimal | None] = mapped_column(MoneyNumeric)
    risk_increase: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )

    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    card_message_id: Mapped[int | None] = mapped_column(BigInteger)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<PositionAction {self.kind} {self.symbol} {self.side} {self.status}>"
