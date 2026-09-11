"""exchange_credentials permissions_checked_at

Раздел 8 ТЗ (проверка прав ключа BingX) — is_read_only перестаёт быть
непроверяемой декларацией пользователя и заполняется реальным ответом
биржи (GET /openApi/v1/account/apiRestrictions). Эта отметка времени
говорит, когда права проверялись последний раз — по ней TTL решает,
пора ли перепроверить (см. app/services/permissions.py).

NULL для всех существующих строк: они заведены до этого шага, права по
ним ещё не проверялись ни разу — это и есть корректное состояние "не
проверено", а не 0 или текущее время.

Revision ID: 4f3837b79361
Revises: b4c1e9a7d203
Create Date: 2026-09-12
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "4f3837b79361"
down_revision = "b4c1e9a7d203"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "exchange_credentials",
        sa.Column("permissions_checked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("exchange_credentials", "permissions_checked_at")
