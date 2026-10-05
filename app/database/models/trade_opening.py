"""Открытие сделки из бота (05.10.2026, M5) — снимок карточки и состояние.

Одна строка — одна показанная карточка «Открыть на бирже» (чат-мастер или
Mini App): что пользователь ввёл, что ему показали (объём, риск с комиссией,
маржа, ликвидация, нарушения плана) и чем кончилось. Её id порождает
идемпотентный client_order_id входа, стопа и аварийного закрытия
(opening_client_order_id): повторное «Открыть», вызов из второго интерфейса и
восстановление после рестарта адресуют ту же строку и не могут отправить
второй вход (UNIQUE на execution_orders.client_order_id). Переходы статуса —
условным UPDATE от ожидаемого (OpeningStatus).

Сделка журнала появляется только после подтверждённого исполнения (trade_id,
SET NULL: открытие переживает удаление сделки).
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
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import (
    Base,
    IntPKMixin,
    MoneyNumeric,
    PercentNumeric,
    PriceNumeric,
    QuantityNumeric,
    RatioNumeric,
    TimestampMixin,
)
from app.trading.enums import (
    EntryType,
    ExchangeKeyMode,
    OpeningSource,
    OpeningStatus,
    TradeSide,
)

_STATUSES = ", ".join(f"'{s.value}'" for s in OpeningStatus)
_ACTIVE = ", ".join(
    f"'{s}'"
    for s in ("CONFIRMED", "SUBMITTING", "UNKNOWN", "WORKING", "FILLED", "PROTECTED", "ALARM")
)


class TradeOpening(IntPKMixin, TimestampMixin, Base):
    __tablename__ = "trade_openings"
    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUSES})", name="status_known"),
        CheckConstraint("entry_type IN ('MARKET', 'LIMIT')", name="entry_type_known"),
        CheckConstraint("source IN ('wizard', 'miniapp')", name="source_known"),
        CheckConstraint("account_mode IN ('LIVE', 'DEMO')", name="account_mode_known"),
        CheckConstraint(
            "entry_type = 'MARKET' OR limit_price IS NOT NULL", name="limit_has_price"
        ),
        Index("ix_trade_openings_user_created", "user_id", "created_at"),
        # Восстановление и цикл лимитов обходят только незавершённые открытия.
        Index(
            "ix_trade_openings_active", "status",
            postgresql_where=text(f"status IN ({_ACTIVE})"),
        ),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[OpeningSource] = mapped_column(
        Enum(
            OpeningSource, native_enum=False, length=8,
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    account_mode: Mapped[ExchangeKeyMode] = mapped_column(
        Enum(ExchangeKeyMode, native_enum=False, length=8), nullable=False
    )
    status: Mapped[OpeningStatus] = mapped_column(
        Enum(OpeningStatus, native_enum=False, length=16),
        default=OpeningStatus.CARD,
        server_default="CARD",
        nullable=False,
    )

    # --- ввод ---------------------------------------------------------------
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[TradeSide] = mapped_column(
        Enum(TradeSide, native_enum=False, length=8), nullable=False
    )
    entry_type: Mapped[EntryType] = mapped_column(
        Enum(EntryType, native_enum=False, length=8), nullable=False
    )
    limit_price: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    stop_loss: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False)
    take_profit: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    risk_percent: Mapped[Decimal] = mapped_column(PercentNumeric, nullable=False)
    leverage: Mapped[int] = mapped_column(Integer, nullable=False)
    margin_type: Mapped[str] = mapped_column(String(8), nullable=False)
    expiry_minutes: Mapped[int | None] = mapped_column(Integer)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- расчёт карточки ----------------------------------------------------
    card_price: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    equity: Mapped[Decimal | None] = mapped_column(MoneyNumeric)
    available: Mapped[Decimal | None] = mapped_column(MoneyNumeric)
    quantity: Mapped[Decimal | None] = mapped_column(QuantityNumeric)
    risk_usd: Mapped[Decimal | None] = mapped_column(MoneyNumeric)
    fee_estimate: Mapped[Decimal | None] = mapped_column(MoneyNumeric)
    margin: Mapped[Decimal | None] = mapped_column(MoneyNumeric)
    rr: Mapped[Decimal | None] = mapped_column(RatioNumeric)
    liq_estimate: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    violations: Mapped[list[dict[str, str]] | None] = mapped_column(JSONB)
    warnings_accepted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )

    # --- исполнение ---------------------------------------------------------
    entry_order_id: Mapped[str | None] = mapped_column(String(64))
    position_id: Mapped[str | None] = mapped_column(String(64))
    filled_qty: Mapped[Decimal | None] = mapped_column(QuantityNumeric)
    avg_price: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    entry_fee: Mapped[Decimal | None] = mapped_column(MoneyNumeric)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trade_id: Mapped[int | None] = mapped_column(
        ForeignKey("trades.id", ondelete="SET NULL")
    )

    # --- сообщение и итог ---------------------------------------------------
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    card_message_id: Mapped[int | None] = mapped_column(BigInteger)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
