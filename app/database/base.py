"""Базовый класс моделей и общие типы колонок.

Вынесен отдельно от session.py, потому что Alembic импортирует metadata
без создания движка — иначе автогенерация миграций требовала бы живого
подключения к БД.

Про числовые типы: все цены, объёмы и деньги — Numeric, никогда не Float.
Float даёт накопление ошибки уже на третьем знаке, а в торговом журнале
расхождение с биржей на копейки означает, что журналу нельзя верить.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, MetaData, Numeric, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Явные имена ограничений: без этого Alembic генерирует миграции с
# автосгенерированными именами, и откат/изменение constraint ломается.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# Цена инструмента. 12 знаков после запятой покрывают монеты вида SHIB.
PriceNumeric = Numeric(28, 12)

# Объём в базовом активе.
QuantityNumeric = Numeric(28, 12)

# Деньги в валюте депозита (USDT). 8 знаков — с запасом на комиссии.
MoneyNumeric = Numeric(20, 8)

# Проценты: риск, PnL %, просадка.
PercentNumeric = Numeric(12, 4)


class TimestampMixin:
    """created_at/updated_at на стороne БД.

    server_default вместо Python-значения: время ставит PostgreSQL, поэтому
    оно консистентно независимо от часового пояса контейнера приложения.
    Всё хранится в UTC, конвертация в таймзону пользователя — на слое отчётов.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class IntPKMixin:
    id: Mapped[int] = mapped_column(primary_key=True)
