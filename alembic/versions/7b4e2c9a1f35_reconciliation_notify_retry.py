"""reconciliation_events: переотправка уведомлений

28.09, уведомления «хотя бы один раз» (app/execution/redelivery.py):

- notify_text — TEXT NULL: полный текст сообщения для переотправки; detail
  остаётся коротким для сводки. У существующих строк NULL — переотправка
  соберёт текст из вида, символа и detail
- attempts — INTEGER NOT NULL DEFAULT 0: число попыток, для диагностики
- last_attempt_at — TIMESTAMPTZ NULL: расписание редкого режима (раз в 10 мин)
- gave_up_at — TIMESTAMPTZ NULL: отказ (старше 24 ч или бот заблокирован)
- частичный индекс ix_reconciliation_events_undelivered (user_id) WHERE
  notified_at IS NULL AND gave_up_at IS NULL — выборка reconciler каждый цикл

Бэкфилла нет: на проде одно событие, notified_at у него стоит.

Новые виды событий (тревоги read-back: STOP_RESCUE_FAILED, STOP_UNVERIFIED,
LIQUIDATION_BEFORE_STOP, ENTRY_PAST_STOP) DDL не требуют — kind VARCHAR(32)
без CHECK. downgrade переводит их в AMBIGUOUS (как PNL_MISMATCH в
19c5c0deedca: старый код падает на незнакомом kind), колонки и индекс
удаляются.

Revision ID: 7b4e2c9a1f35
Revises: 19c5c0deedca
Create Date: 2026-09-28
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "7b4e2c9a1f35"
down_revision = "19c5c0deedca"
branch_labels = None
depends_on = None

_NEW_KINDS = (
    "STOP_RESCUE_FAILED", "STOP_UNVERIFIED", "LIQUIDATION_BEFORE_STOP", "ENTRY_PAST_STOP",
)


def upgrade() -> None:
    op.add_column("reconciliation_events", sa.Column("notify_text", sa.Text(), nullable=True))
    op.add_column(
        "reconciliation_events",
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "reconciliation_events",
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "reconciliation_events",
        sa.Column("gave_up_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_reconciliation_events_undelivered",
        "reconciliation_events",
        ["user_id"],
        postgresql_where=sa.text("notified_at IS NULL AND gave_up_at IS NULL"),
    )


def downgrade() -> None:
    kinds = ", ".join(f"'{kind}'" for kind in _NEW_KINDS)
    op.execute(f"UPDATE reconciliation_events SET kind='AMBIGUOUS' WHERE kind IN ({kinds})")
    op.drop_index("ix_reconciliation_events_undelivered", table_name="reconciliation_events")
    op.drop_column("reconciliation_events", "gave_up_at")
    op.drop_column("reconciliation_events", "last_attempt_at")
    op.drop_column("reconciliation_events", "attempts")
    op.drop_column("reconciliation_events", "notify_text")
