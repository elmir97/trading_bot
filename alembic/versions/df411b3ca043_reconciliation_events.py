"""reconciliation_events, статус NOT_PLACED

Шаг 15.6, reconciler. Таблица событий сверки журнала с биржей: закрытия
сделок фактом биржи, разрешённые UNKNOWN/PENDING входы и расхождения без
однозначного факта. Частичный UNIQUE (user_id, dedup_key) по строкам без
resolved_at — одно уведомление на открытое расхождение, повторное
появление после разрешения даёт новую строку.

Статус execution_orders NOT_PLACED («ордер не выставлен», ставит только
reconciler после окна) миграции не требует: status — VARCHAR(16) без CHECK.

downgrade: старый код не знает NOT_PLACED (Enum(native_enum=False)
поднимает LookupError, падает сводка) — такие строки переводятся в UNKNOWN,
прежнюю семантику «исход неизвестен». Таблица событий удаляется вместе с
историей сверок.

Revision ID: df411b3ca043
Revises: 407583974eb1
Create Date: 2026-09-27
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "df411b3ca043"
down_revision = "407583974eb1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reconciliation_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("trade_id", sa.Integer(), nullable=True),
        sa.Column("execution_order_id", sa.Integer(), nullable=True),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("dedup_key", sa.String(length=128), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_reconciliation_events_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["trade_id"], ["trades.id"], name=op.f("fk_reconciliation_events_trade_id_trades"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["execution_order_id"], ["execution_orders.id"],
            name=op.f("fk_reconciliation_events_execution_order_id_execution_orders"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reconciliation_events")),
    )
    op.create_index(
        "uq_reconciliation_events_open",
        "reconciliation_events",
        ["user_id", "dedup_key"],
        unique=True,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )
    op.create_index(
        "ix_reconciliation_events_user_created",
        "reconciliation_events",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.execute("UPDATE execution_orders SET status='UNKNOWN' WHERE status='NOT_PLACED'")
    op.drop_index("ix_reconciliation_events_user_created", table_name="reconciliation_events")
    op.drop_index("uq_reconciliation_events_open", table_name="reconciliation_events")
    op.drop_table("reconciliation_events")
