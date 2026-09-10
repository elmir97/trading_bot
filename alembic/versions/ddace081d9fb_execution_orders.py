"""execution_orders table, trades.signal_id, signals.trade_opened_at

Этап 15.1 — миграция + модели + репозитории для исполнения сделок по
подтверждению. Ничего не исполняется: таблица только фиксирует ордера,
которые код (этапы 15.4-15.5) впоследствии станет отправлять на биржу.

trades.source получает новое значение SIGNAL_EXECUTION — колонка хранится
как VARCHAR(16) без CHECK-ограничения (см. app/trading/enums.py), поэтому
само добавление значения не требует ALTER TABLE.

Revision ID: ddace081d9fb
Revises: 9504669824c3
Create Date: 2026-09-08
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "ddace081d9fb"
down_revision = "9504669824c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "signals",
        sa.Column("trade_opened_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "trades",
        sa.Column("signal_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_trades_signal_id_signals",
        "trades",
        "signals",
        ["signal_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "execution_orders",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("signal_id", sa.Integer(), nullable=True),
        sa.Column("trade_id", sa.Integer(), nullable=True),
        sa.Column("client_order_id", sa.String(length=64), nullable=False),
        sa.Column("exchange_order_id", sa.String(length=64), nullable=True),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("position_side", sa.String(length=8), nullable=False),
        sa.Column("order_type", sa.String(length=24), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=28, scale=12), nullable=False),
        sa.Column("price", sa.Numeric(precision=28, scale=12), nullable=True),
        sa.Column("trigger_price", sa.Numeric(precision=28, scale=12), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column("raw_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_execution_orders_user_id_users", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["signal_id"], ["signals.id"],
            name="fk_execution_orders_signal_id_signals", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["trade_id"], ["trades.id"],
            name="fk_execution_orders_trade_id_trades", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_execution_orders"),
        sa.UniqueConstraint(
            "client_order_id", name="uq_execution_orders_client_order_id"
        ),
    )
    op.create_index(
        "ix_execution_orders_user_status", "execution_orders", ["user_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_execution_orders_user_status", table_name="execution_orders")
    op.drop_table("execution_orders")

    op.drop_constraint("fk_trades_signal_id_signals", "trades", type_="foreignkey")
    op.drop_column("trades", "signal_id")

    op.drop_column("signals", "trade_opened_at")
