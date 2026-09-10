"""Сделка и её исполнения — ядро журнала.

Ключевое архитектурное решение: источник истины — TradeFill, а поля
Trade (средняя цена входа, объём, PnL) — пересчитываемые агрегаты.

Зачем так, если в UI сделка выглядит как «один вход — один выход»:
частичные входы и частичные фиксации есть у любого, кто торгует всерьёз,
а автоимпорт с биржи приходит именно исполнениями. Добавить эту таблицу
позже означало бы переписать журнал, статистику и мигрировать историю.
Сейчас это стоит одной дополнительной таблицы.

Про плечо: оно НЕ участвует в расчёте PnL. Формула —
    (exit - entry) * quantity * direction - fees
где quantity в базовом активе. Плечо влияет только на размер маржи и на
проверку лимитов. Умножение PnL на плечо — самая частая ошибка в таких
журналах, и она даёт цифры, расходящиеся с биржей в разы.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import (
    Base,
    IntPKMixin,
    MoneyNumeric,
    PercentNumeric,
    PriceNumeric,
    QuantityNumeric,
    TimestampMixin,
)
from app.trading.enums import FillSide, TradeSide, TradeSource, TradeStatus

if TYPE_CHECKING:
    from app.database.models.mistake import TradeMistake
    from app.database.models.signal import SignalRecord
    from app.database.models.strategy import Strategy
    from app.database.models.user import User


class Trade(IntPKMixin, TimestampMixin, Base):
    __tablename__ = "trades"
    __table_args__ = (
        # Основные срезы статистики. Без этих индексов отчёты за месяц
        # начнут делать seq scan уже на нескольких тысячах сделок.
        Index("ix_trades_user_status", "user_id", "status"),
        Index("ix_trades_user_closed", "user_id", "closed_at"),
        Index("ix_trades_user_symbol", "user_id", "symbol"),
        Index("ix_trades_user_strategy", "user_id", "strategy_id"),
        UniqueConstraint(
            "user_id", "exchange", "external_position_id",
            name="uq_trade_external_position",
        ),
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        CheckConstraint("leverage >= 1", name="leverage_at_least_one"),
        CheckConstraint(
            "(status != 'CLOSED') OR (closed_at IS NOT NULL)",
            name="closed_trade_has_closed_at",
        ),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    # --- Идентификация инструмента ----------------------------------------
    exchange: Mapped[str] = mapped_column(String(32), default="bingx", nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[TradeSide] = mapped_column(
        Enum(TradeSide, native_enum=False, length=8), nullable=False
    )

    # --- Агрегаты, пересчитываемые из fills -------------------------------
    entry_price: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    exit_price: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    quantity: Mapped[Decimal] = mapped_column(
        QuantityNumeric, default=Decimal(0), nullable=False
    )
    fees: Mapped[Decimal] = mapped_column(
        MoneyNumeric, default=Decimal(0), nullable=False
    )

    # --- Параметры риска, заданные при входе ------------------------------
    stop_loss: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    take_profit: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    leverage: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # --- Монитор приближения к TP/SL (этап 12) -----------------------------
    # Момент последнего уведомления "цена рядом с уровнем". NULL — либо ещё
    # не приближалась, либо снова отъехала и предупреждение можно слать
    # заново при новом подходе. Отдельного fingerprint не нужно: у сделки
    # ровно один TP и один SL, дедуп — не то же самое, что у сканера сетапов.
    tp_approach_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sl_approach_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- Результат (считается кодом, не вводится руками) ------------------
    pnl: Mapped[Decimal | None] = mapped_column(MoneyNumeric)
    pnl_percent: Mapped[Decimal | None] = mapped_column(PercentNumeric)
    risk_percent: Mapped[Decimal | None] = mapped_column(PercentNumeric)
    risk_reward: Mapped[Decimal | None] = mapped_column(PercentNumeric)

    # Баланс на момент открытия — нужен, чтобы risk % и PnL % оставались
    # корректными задним числом. Текущий баланс для этого не годится.
    account_balance_at_entry: Mapped[Decimal | None] = mapped_column(MoneyNumeric)

    # --- Разметка пользователя --------------------------------------------
    strategy_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategies.id", ondelete="SET NULL")
    )
    timeframe: Mapped[str | None] = mapped_column(String(8))
    entry_reason: Mapped[str | None] = mapped_column(Text)
    exit_reason: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    # --- Служебное ---------------------------------------------------------
    status: Mapped[TradeStatus] = mapped_column(
        Enum(TradeStatus, native_enum=False, length=16),
        default=TradeStatus.OPEN,
        nullable=False,
    )
    source: Mapped[TradeSource] = mapped_column(
        Enum(TradeSource, native_enum=False, length=16),
        default=TradeSource.MANUAL,
        nullable=False,
    )
    # Этап 15: сигнал, по которому бот открыл эту сделку. NULL для ручных
    # и импортированных сделок — заполняется только при source=SIGNAL_EXECUTION.
    signal_id: Mapped[int | None] = mapped_column(
        ForeignKey("signals.id", ondelete="SET NULL")
    )
    # Импортированная сделка до разметки не участвует в статистике по
    # стратегиям и ошибкам — иначе срезы окажутся смещёнными.
    is_annotated: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    external_position_id: Mapped[str | None] = mapped_column(String(64))

    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- Связи -------------------------------------------------------------
    user: Mapped[User] = relationship(back_populates="trades")
    strategy: Mapped[Strategy | None] = relationship(back_populates="trades")
    signal: Mapped[SignalRecord | None] = relationship()
    fills: Mapped[list[TradeFill]] = relationship(
        back_populates="trade",
        cascade="all, delete-orphan",
        order_by="TradeFill.executed_at",
        # selectin, а не ленивая загрузка: любой пересчёт агрегатов читает
        # fills целиком, а ленивое обращение в async-режиме падает с
        # MissingGreenlet вместо тихой подгрузки.
        lazy="selectin",
    )
    mistakes: Mapped[list[TradeMistake]] = relationship(
        back_populates="trade", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def is_open(self) -> bool:
        return self.status is TradeStatus.OPEN

    @property
    def is_win(self) -> bool | None:
        """None для незакрытых сделок — отличается от 'убыточная'."""
        if self.pnl is None or self.status is not TradeStatus.CLOSED:
            return None
        return self.pnl > 0

    def __repr__(self) -> str:
        return f"<Trade {self.id} {self.symbol} {self.side} {self.status}>"


class TradeFill(IntPKMixin, Base):
    """Отдельное исполнение. Источник истины для всех агрегатов сделки."""

    __tablename__ = "trade_fills"
    __table_args__ = (
        # Защита от дублей при повторном импорте: биржа может отдать один
        # и тот же fill в пересекающихся временных окнах.
        UniqueConstraint(
            "user_id", "exchange", "external_fill_id",
            name="uq_fill_external_id",
        ),
        Index("ix_fills_trade", "trade_id", "executed_at"),
        CheckConstraint("quantity > 0", name="fill_quantity_positive"),
        CheckConstraint("price > 0", name="fill_price_positive"),
    )

    trade_id: Mapped[int] = mapped_column(
        ForeignKey("trades.id", ondelete="CASCADE"), nullable=False
    )
    # Денормализован намеренно: позволяет проверять изоляцию пользователя
    # в UNIQUE-ограничении без join к trades.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    exchange: Mapped[str] = mapped_column(String(32), default="bingx", nullable=False)

    fill_side: Mapped[FillSide] = mapped_column(
        Enum(FillSide, native_enum=False, length=8), nullable=False
    )
    price: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(QuantityNumeric, nullable=False)
    fee: Mapped[Decimal] = mapped_column(
        MoneyNumeric, default=Decimal(0), nullable=False
    )
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    external_fill_id: Mapped[str | None] = mapped_column(String(64))

    trade: Mapped[Trade] = relationship(back_populates="fills")

    def __repr__(self) -> str:
        return f"<TradeFill {self.fill_side} {self.quantity}@{self.price}>"
