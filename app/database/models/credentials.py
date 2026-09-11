"""Биржевые API-ключи.

В базе лежат только шифротексты Fernet. Расшифровка происходит в памяти
процесса непосредственно перед подписью запроса и никуда не сохраняется.
Поле api_key_masked существует, чтобы показать пользователю, какой ключ
подключён, не расшифровывая ничего.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, IntPKMixin, TimestampMixin
from app.trading.enums import ExchangeKeyMode

if TYPE_CHECKING:
    from app.database.models.user import User


class ExchangeCredentials(IntPKMixin, TimestampMixin, Base):
    __tablename__ = "exchange_credentials"
    __table_args__ = (
        # Этап 15.4в: пара ключей на каждый режим (LIVE/DEMO), не одна на
        # биржу — иначе демо-ключ некуда положить, не затерев боевой.
        UniqueConstraint("user_id", "exchange", "mode", name="uq_user_exchange_mode"),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[ExchangeKeyMode] = mapped_column(
        Enum(ExchangeKeyMode, native_enum=False, length=8),
        default=ExchangeKeyMode.LIVE,
        nullable=False,
    )

    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    api_secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    api_key_masked: Mapped[str] = mapped_column(String(64), nullable=False)

    # Раньше было декларацией пользователя ("я обещаю, что ключ read-only"),
    # непроверяемой по API. Теперь заполняется реальным ответом биржи
    # (GET /openApi/v1/account/apiRestrictions, см. app/services/permissions.py)
    # — is_read_only = not enable_futures. Дефолт True остаётся: до первой
    # успешной проверки безопаснее считать, что торговать нельзя.
    is_read_only: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # NULL — права ещё не проверялись ни разу (новый ключ). Раздел 8 ТЗ,
    # этап проверки прав: TTL решает, пора ли перепроверить (см.
    # app/services/permissions.py, Settings.exec_permissions_ttl_hours).
    permissions_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="credentials")

    def __repr__(self) -> str:
        return f"<ExchangeCredentials {self.exchange} {self.mode.value} {self.api_key_masked}>"
