"""Торговый план — свод правил, против которых проверяется каждая сделка.

Бот предупреждает о нарушении, но не блокирует действие: запрет породил бы
привычку вести журнал мимо бота, и данные перестали бы отражать реальность.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import String

from app.database.base import Base, IntPKMixin, PercentNumeric, TimestampMixin

if TYPE_CHECKING:
    from app.database.models.user import User


class TradingPlan(IntPKMixin, TimestampMixin, Base):
    __tablename__ = "trading_plans"
    __table_args__ = (
        CheckConstraint(
            "risk_per_trade_percent > 0 AND risk_per_trade_percent <= 100",
            name="risk_per_trade_range",
        ),
        CheckConstraint(
            "max_daily_loss_percent > 0", name="max_daily_loss_positive"
        ),
        CheckConstraint("min_risk_reward > 0", name="min_rr_positive"),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )

    risk_per_trade_percent: Mapped[Decimal] = mapped_column(
        PercentNumeric, default=Decimal("2.0"), nullable=False
    )
    max_daily_loss_percent: Mapped[Decimal] = mapped_column(
        PercentNumeric, default=Decimal("6.0"), nullable=False
    )
    max_weekly_loss_percent: Mapped[Decimal] = mapped_column(
        PercentNumeric, default=Decimal("10.0"), nullable=False
    )
    max_trades_per_day: Mapped[int] = mapped_column(
        Integer, default=5, nullable=False
    )
    min_risk_reward: Mapped[Decimal] = mapped_column(
        PercentNumeric, default=Decimal("2.0"), nullable=False
    )
    max_leverage: Mapped[int] = mapped_column(Integer, default=10, nullable=False)

    # Списки строк, а не отдельные таблицы: связей у них нет, читаются
    # всегда целиком, редактируются пользователем как единое поле.
    allowed_symbols: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), default=list, server_default="{}", nullable=False
    )
    allowed_timeframes: Mapped[list[str]] = mapped_column(
        ARRAY(String(8)), default=list, server_default="{}", nullable=False
    )

    user: Mapped[User] = relationship(back_populates="trading_plan")


# Топ-10 монет по объёму на фьючерсах.
#
# Тикеры записаны так, как их отдаёт API BingX (/openApi/swap/v2/quote/
# contracts, поле symbol), а не так, как биржа показывает их в интерфейсе:
# Toncoin там торгуется под тикером GRAMTON-USDT (asset "GRAMTON"; в
# интерфейсе биржа подписывает эту же пару как «GRAM-USDT» — это только
# displayName, самого символа GRAM-USDT в списке контрактов нет), а не
# TON-USDT и не TONCOIN-USDT, как на большинстве бирж. Ошибка в имени не
# ломает бота, но инструмент молча выпадает из цен и анализа.
DEFAULT_ALLOWED_SYMBOLS = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT",
    "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "LINK-USDT", "GRAMTON-USDT",
]

DEFAULT_ALLOWED_TIMEFRAMES = ["1h", "4h"]
