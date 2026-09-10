"""signals table, TP/SL approach tracking, daily notification dates

Этап 12 — фоновые задачи и уведомления.

Revision ID: 9504669824c3
Revises: a1f7c3d90e11
Create Date: 2026-09-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "9504669824c3"
down_revision = "a1f7c3d90e11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=8), nullable=False),
        sa.Column("level", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=8), nullable=False, server_default="ACTIVE"),
        sa.Column("setup", sa.String(length=64), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("entry_low", sa.Numeric(precision=28, scale=12), nullable=True),
        sa.Column("entry_high", sa.Numeric(precision=28, scale=12), nullable=True),
        sa.Column("stop_loss", sa.Numeric(precision=28, scale=12), nullable=True),
        sa.Column("take_profit", sa.Numeric(precision=28, scale=12), nullable=True),
        sa.Column("confidence", sa.SmallInteger(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "symbol", "timeframe", "level", name="uq_signal_slot"
        ),
    )
    op.create_index("ix_signals_user_status", "signals", ["user_id", "status"])

    op.add_column(
        "trades",
        sa.Column("tp_approach_notified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "trades",
        sa.Column("sl_approach_notified_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "user_settings",
        sa.Column("daily_summary_last_sent_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "user_settings",
        sa.Column("daily_loss_alert_last_sent_date", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "daily_loss_alert_last_sent_date")
    op.drop_column("user_settings", "daily_summary_last_sent_date")

    op.drop_column("trades", "sl_approach_notified_at")
    op.drop_column("trades", "tp_approach_notified_at")

    op.drop_index("ix_signals_user_status", table_name="signals")
    op.drop_table("signals")
