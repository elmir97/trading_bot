"""Стратегия пользователя.

Отдельная сущность, а не строка в trades: иначе опечатка в названии
разобьёт статистику на две «разные» стратегии, а переименование
потребует UPDATE по всей истории.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, IntPKMixin, TimestampMixin

if TYPE_CHECKING:
    from app.database.models.trade import Trade
    from app.database.models.user import User


class Strategy(IntPKMixin, TimestampMixin, Base):
    __tablename__ = "strategies"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_user_strategy_name"),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    rules: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    user: Mapped[User] = relationship(back_populates="strategies")
    trades: Mapped[list[Trade]] = relationship(back_populates="strategy")

    def __repr__(self) -> str:
        return f"<Strategy {self.name}>"


# Стратегии из методички, создаются при регистрации пользователя.
DEFAULT_STRATEGIES: list[dict[str, str]] = [
    {
        "name": "Пробой с ретестом",
        "description": "Вход после подтверждённого пробоя уровня и его ретеста.",
        "rules": (
            "1. Закрытие полнотелой свечи за уровнем, объём выше среднего.\n"
            "2. Следующая свеча закрывается за уровнем (правило двух свечей).\n"
            "3. Возврат цены к пробитому уровню.\n"
            "4. Подтверждающий паттерн: пин-бар или поглощение.\n"
            "5. Вход по закрытию подтверждающей свечи, SL за её хвост.\n"
            "6. Направление совпадает с положением цены относительно EMA200.\n"
            "7. RR не менее 1:2."
        ),
    },
    {
        "name": "Откат к EMA50",
        "description": "Вход по тренду на откате к динамической поддержке.",
        "rules": (
            "1. Структура рынка трендовая (HH/HL или LH/LL).\n"
            "2. Цена по нужную сторону EMA200.\n"
            "3. Откат к зоне EMA50.\n"
            "4. Подтверждающий разворотный паттерн в зоне.\n"
            "5. SL за локальный экстремум отката.\n"
            "6. RR не менее 1:2."
        ),
    },
    {
        "name": "Вход после новости",
        "description": "Вход на первом откате после того, как рынок определил направление.",
        "rules": (
            "1. За 15-30 минут до новости не входить, открытые позиции в безубыток.\n"
            "2. Первые 15 минут после выхода данных — бездействие.\n"
            "3. Дождаться формирования структуры (15-30 минут).\n"
            "4. Вход на первом откате к уровню или EMA20/50.\n"
            "5. SL за локальный экстремум коррекции."
        ),
    },
]
