"""Классификация торговых ошибок.

Справочник в таблице, а не Enum в коде: список должен расширяться самим
пользователем без миграции и деплоя. Системные записи (is_system=True)
создаются при регистрации и не удаляются.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, IntPKMixin, TimestampMixin

if TYPE_CHECKING:
    from app.database.models.trade import Trade


class MistakeType(IntPKMixin, TimestampMixin, Base):
    __tablename__ = "mistake_types"
    __table_args__ = (
        UniqueConstraint("user_id", "code", name="uq_user_mistake_code"),
    )

    # NULL = системный справочник, общий для всех.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    def __repr__(self) -> str:
        return f"<MistakeType {self.code}>"


class TradeMistake(IntPKMixin, Base):
    """Связь сделки с ошибкой. Одна сделка может нести несколько ошибок."""

    __tablename__ = "trade_mistakes"
    __table_args__ = (
        UniqueConstraint("trade_id", "mistake_type_id", name="uq_trade_mistake"),
    )

    trade_id: Mapped[int] = mapped_column(
        ForeignKey("trades.id", ondelete="CASCADE"), index=True, nullable=False
    )
    mistake_type_id: Mapped[int] = mapped_column(
        ForeignKey("mistake_types.id", ondelete="CASCADE"), index=True, nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text)

    trade: Mapped[Trade] = relationship(back_populates="mistakes")
    mistake_type: Mapped[MistakeType] = relationship(lazy="joined")


SYSTEM_MISTAKES: list[dict[str, str]] = [
    {"code": "FOMO", "title": "FOMO",
     "description": "Вход из страха упустить движение, вне плана."},
    {"code": "EARLY_ENTRY", "title": "Ранний вход",
     "description": "Вход до подтверждения — без ретеста или сигнальной свечи."},
    {"code": "LATE_ENTRY", "title": "Поздний вход",
     "description": "Вход после того, как движение уже прошло, SL далеко."},
    {"code": "NO_STOP_LOSS", "title": "Без стоп-лосса",
     "description": "Сделка открыта без защитного ордера."},
    {"code": "MOVED_STOP", "title": "Двигал стоп",
     "description": "Стоп отодвинут от цены в надежде на разворот."},
    {"code": "OVERSIZED_POSITION", "title": "Превышен размер позиции",
     "description": "Риск выше допустимого по торговому плану."},
    {"code": "RULE_VIOLATION", "title": "Нарушение правил",
     "description": "Сделка вне стратегии, инструмента или таймфрейма плана."},
    {"code": "REVENGE_TRADE", "title": "Отыгрыш",
     "description": "Вход сразу после убытка ради его возврата."},
    {"code": "EARLY_EXIT", "title": "Ранний выход",
     "description": "Закрытие до цели без причины со стороны рынка."},
    {"code": "OVERTRADING", "title": "Перебор сделок",
     "description": "Превышен дневной лимит количества сделок."},
    {"code": "AGAINST_TREND", "title": "Против тренда",
     "description": "Вход против направления по EMA200 на старшем ТФ."},
    {"code": "LOW_RR", "title": "Низкий RR",
     "description": "Вход с соотношением риск/прибыль ниже допустимого."},
    {"code": "NEWS_TRADE", "title": "Вход на новости",
     "description": "Вход в окне повышенной волатильности вокруг новости."},
]
