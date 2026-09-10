"""Биржевые API-ключи.

В базе лежат только шифротексты Fernet. Расшифровка происходит в памяти
процесса непосредственно перед подписью запроса и никуда не сохраняется.
Поле api_key_masked существует, чтобы показать пользователю, какой ключ
подключён, не расшифровывая ничего.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, IntPKMixin, TimestampMixin

if TYPE_CHECKING:
    from app.database.models.user import User


class ExchangeCredentials(IntPKMixin, TimestampMixin, Base):
    __tablename__ = "exchange_credentials"
    __table_args__ = (
        UniqueConstraint("user_id", "exchange", name="uq_user_exchange"),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)

    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    api_secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    api_key_masked: Mapped[str] = mapped_column(String(64), nullable=False)

    # Пользователь подтверждает, что ключ read-only. Проверить это по API
    # надёжно нельзя, поэтому храним как декларацию и предупреждаем при false.
    is_read_only: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    user: Mapped[User] = relationship(back_populates="credentials")

    def __repr__(self) -> str:
        return f"<ExchangeCredentials {self.exchange} {self.api_key_masked}>"
