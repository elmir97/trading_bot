"""Ордера, отправленные на биржу при исполнении сигнала (этап 15).

Одна подтверждённая сделка порождает до трёх строк — вход, стоп, тейк —
каждая со своим client_order_id. Это единственный источник истины для
идемпотентности (раздел 8 ТЗ): прежде чем что-либо отправить на биржу,
код обязан записать сюда строку в статусе PENDING и закоммитить её, и
только потом идти в HTTP. Иначе обрыв связи между "отправили" и "получили
ответ" оставляет отправку неотличимой от несостоявшейся, и повтор
превращается во вторую позицию.

Журнал (Trade/TradeFill) остаётся источником истории сделки для человека;
эта таблица — источник истины для "что именно и в каком статусе мы
попросили биржу сделать", нужный коду, а не отчётам.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Enum, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import (
    Base,
    IntPKMixin,
    PriceNumeric,
    QuantityNumeric,
    TimestampMixin,
)
from app.trading.enums import OrderRole, OrderSide, OrderStatus, OrderType, TradeSide

if TYPE_CHECKING:
    from app.database.models.signal import SignalRecord
    from app.database.models.trade import Trade
    from app.database.models.user import User


class ExecutionOrder(IntPKMixin, TimestampMixin, Base):
    __tablename__ = "execution_orders"
    __table_args__ = (
        Index("ix_execution_orders_user_status", "user_id", "status"),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # SET NULL, а не CASCADE: сигнал и сделка — контекст, а не владелец
    # записи об ордере. Ордер как факт обращения к бирже должен пережить
    # удаление сигнала (истёк и вычищен) или сделки.
    signal_id: Mapped[int | None] = mapped_column(
        ForeignKey("signals.id", ondelete="SET NULL")
    )
    trade_id: Mapped[int | None] = mapped_column(
        ForeignKey("trades.id", ondelete="SET NULL")
    )

    # Детерминированный ключ идемпотентности: f"tj{signal_id}{user_id}{role}".
    # Точный лимит длины и допустимые символы у BingX — проверить перед
    # 15.5 (раздел 16 ТЗ), 64 символа взяты с запасом.
    client_order_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )
    exchange_order_id: Mapped[str | None] = mapped_column(String(64))

    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[OrderSide] = mapped_column(
        Enum(OrderSide, native_enum=False, length=8), nullable=False
    )
    # LONG/SHORT — та же сторона позиции, что и у Trade.side, а не BUY/SELL.
    position_side: Mapped[TradeSide] = mapped_column(
        Enum(TradeSide, native_enum=False, length=8), nullable=False
    )
    order_type: Mapped[OrderType] = mapped_column(
        Enum(OrderType, native_enum=False, length=24), nullable=False
    )
    role: Mapped[OrderRole] = mapped_column(
        Enum(OrderRole, native_enum=False, length=16), nullable=False
    )

    quantity: Mapped[Decimal] = mapped_column(QuantityNumeric, nullable=False)
    # Маркет-вход не задаёт цену — только количество; условные SL/TP её
    # тоже обычно не имеют (закрывают по рынку), отсюда nullable.
    price: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    # Цена срабатывания для STOP_MARKET/TAKE_PROFIT_MARKET. NULL у ENTRY.
    trigger_price: Mapped[Decimal | None] = mapped_column(PriceNumeric)

    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, native_enum=False, length=16),
        default=OrderStatus.PENDING,
        server_default="PENDING",
        nullable=False,
    )

    raw_response: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship()
    signal: Mapped[SignalRecord | None] = relationship()
    trade: Mapped[Trade | None] = relationship()

    def __repr__(self) -> str:
        return (
            f"<ExecutionOrder {self.role} {self.symbol} "
            f"{self.status} client_order_id={self.client_order_id!r}>"
        )
