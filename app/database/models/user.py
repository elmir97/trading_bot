"""Пользователь и его настройки.

Бот персональный, но вся модель данных многопользовательская с первого дня:
у каждой сущности есть user_id, и репозитории обязаны по нему фильтровать.
Переход к нескольким пользователям не потребует изменения схемы.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, Date, Enum, ForeignKey, SmallInteger, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, IntPKMixin, TimestampMixin
from app.trading.enums import ExchangeKeyMode

if TYPE_CHECKING:
    from app.database.models.ai_report import AIReport
    from app.database.models.credentials import ExchangeCredentials
    from app.database.models.strategy import Strategy
    from app.database.models.trade import Trade
    from app.database.models.trading_plan import TradingPlan


class User(IntPKMixin, TimestampMixin, Base):
    __tablename__ = "users"

    # BigInteger обязателен: Telegram ID уже вышел за пределы int32.
    telegram_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True, nullable=False
    )
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    settings: Mapped[UserSettings] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    trading_plan: Mapped[TradingPlan | None] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    trades: Mapped[list[Trade]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    strategies: Mapped[list[Strategy]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    credentials: Mapped[list[ExchangeCredentials]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    ai_reports: Mapped[list[AIReport]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} tg={self.telegram_id}>"


class UserSettings(IntPKMixin, TimestampMixin, Base):
    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )

    timezone: Mapped[str] = mapped_column(
        String(64), default="Asia/Yekaterinburg", nullable=False
    )
    quote_currency: Mapped[str] = mapped_column(
        String(16), default="USDT", nullable=False
    )
    language: Mapped[str] = mapped_column(String(8), default="ru", nullable=False)

    # Этап 15.4в: какой счёт бот ЧИТАЕТ и ПОКАЗЫВАЕТ (баланс, позиции, карточка
    # подтверждения) — не то, куда реально уходят ордера (это конфиг, см.
    # Settings.bingx_trading_mode и guards.check_mode_allowed). По умолчанию
    # LIVE (решение пользователя, раздел "Модель данных" шага 15.4в).
    active_exchange_mode: Mapped[ExchangeKeyMode] = mapped_column(
        Enum(ExchangeKeyMode, native_enum=False, length=8),
        default=ExchangeKeyMode.LIVE,
        server_default="LIVE",
        nullable=False,
    )

    # JSONB, а не колонки: набор типов уведомлений будет расти, и каждый новый
    # не должен требовать миграции. Это действительно динамические данные.
    # Сюда же в будущем лягут тихие часы (quiet_hours_start/end, этап 12,
    # не реализованы) — JSONB не потребует под них новой миграции.
    notifications: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )

    # --- Дневные уведомления (этап 12) --------------------------------------
    # Дата последней отправки по локальному дню пользователя — не бьётся по
    # времени процесса, только по календарному дню в его часовом поясе.
    # NULL значит "ещё не отправляли".
    daily_summary_last_sent_date: Mapped[date | None] = mapped_column(Date)
    daily_loss_alert_last_sent_date: Mapped[date | None] = mapped_column(Date)
    # Этап 15.4, раздел 12а — та же схема "не чаще раза в локальный день".
    execution_digest_last_sent_date: Mapped[date | None] = mapped_column(Date)

    # --- Приближение к SL/TP (этап 5, M2) -------------------------------------
    # Порог — доля пути от входа к уровню, в процентах (50–95, шаг 5). NULL —
    # по умолчанию DEFAULT_ALERT_PERCENT. Вкл/выкл — ключи sl_approaching /
    # tp_approaching в notifications (без ключа — старый tp_sl_approaching).
    sl_alert_percent: Mapped[int | None] = mapped_column(SmallInteger)
    tp_alert_percent: Mapped[int | None] = mapped_column(SmallInteger)

    user: Mapped[User] = relationship(back_populates="settings")


# Этап 5: порог уведомления о приближении — пройдено 80% пути до уровня.
DEFAULT_ALERT_PERCENT = 80
ALERT_PERCENT_CHOICES = tuple(range(50, 100, 5))

DEFAULT_NOTIFICATIONS: dict[str, bool] = {
    "position_opened": True,
    "position_closed": True,
    "take_profit_hit": True,
    "stop_loss_hit": True,
    "daily_limit_reached": True,
    # Этап 5: приближение к SL / TP — раздельно. Старый общий ключ
    # tp_sl_approaching в JSONB не трогаем: без новых ключей читается он
    # (app/workers/notifier.approach_enabled).
    "sl_approaching": True,
    "tp_approaching": True,
    "daily_report": True,
    "unannotated_trades": True,
    "execution_digest": True,  # этап 15.4: ежедневная сводка исполнения (раздел 12а)
}
