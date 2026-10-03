"""M4: отсечка журнала (user_settings.journal_cutoff_at)

- user_settings.journal_cutoff_at — TIMESTAMPTZ, nullable, без бэкфилла:
  NULL — отсечки нет. Исполнения раньше отсечки /import, «В журнал» и
  reconciler в журнал не заводят (журнал очищен 03.10.2026 20:38 UTC —
  без отсечки /import за 30 дней вернул бы удалённые сделки).

Значение владельцу (2026-10-03 20:38:34 UTC) ставится отдельным UPDATE после
деплоя, по «да» — в миграции нет даты, привязанной к данным прода.

Репетиция: --checksum --expect-columns user_settings.journal_cutoff_at
--expect-null user_settings.journal_cutoff_at.

downgrade: колонка удаляется; отсечка теряется.

Revision ID: 3f7a9c1e5d20
Revises: 8c41d2e7b9a3
Create Date: 2026-10-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "3f7a9c1e5d20"
down_revision = "8c41d2e7b9a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("journal_cutoff_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "journal_cutoff_at")
