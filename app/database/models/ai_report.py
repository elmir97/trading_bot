"""Таблица сохранённых AI-отчётов.

Три задачи: кэш (не платить дважды за один набор фактов), учёт расходов
и воспроизводимость — видно, на каких именно фактах модель строила вывод.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


class AIReport(Base):
    __tablename__ = "ai_reports"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    # sha256 от FactPack — ключ кэша.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    facts_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    report_json: Mapped[dict] = mapped_column(JSONB, nullable=False)

    model: Mapped[str] = mapped_column(String(64), nullable=False)
    input_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    output_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0"), server_default="0"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )

    user = relationship("User", back_populates="ai_reports")

    __table_args__ = (
        Index("ix_ai_reports_user_fingerprint", "user_id", "fingerprint", unique=True),
        Index("ix_ai_reports_user_created", "user_id", "created_at"),
    )
