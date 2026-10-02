"""M1: position_actions и поля под управление позициями (этапы 3–4)

- position_actions — снимок карточки действия с позицией (перенос стопа,
  тейк, частичное и полное закрытие): позиция биржи, параметры, цифры
  карточки, статус. Новая таблица, пустая.
- trades.initial_stop_loss — первый известный стоп, база 1R (stop_loss с
  этапа 3 следует за стопом на бирже). Nullable, без бэкфилла: NULL — R от
  stop_loss, как раньше.
- execution_orders.position_action_id — FK на position_actions (SET NULL).
  Nullable, без бэкфилла.
- execution_callbacks: action String(8) → String(16) (pm_yes_risk — 11
  символов), CHECK расширен на pm_open/pm_yes/pm_yes_risk/pm_no,
  position_action_id (без FK, как notification_id). Существующие строки не
  меняются.

Репетиция: --checksum --expect-null trades.initial_stop_loss,
execution_orders.position_action_id,execution_callbacks.position_action_id
--allow-count-change position_actions.

downgrade: нажатия pm_* удаляются (старый CHECK их не допускает, старый код
их не знает), затем всё обратно; position_actions удаляется вместе со
строками. Без строк pm_* (до этапа 4) данные не меняются.

Revision ID: 5951467e6d9a
Revises: 0bf103d4a84d
Create Date: 2026-10-02
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "5951467e6d9a"
down_revision = "0bf103d4a84d"
branch_labels = None
depends_on = None

_OLD_ACTIONS = "action IN ('open', 'yes', 'no')"
_NEW_ACTIONS = "action IN ('open', 'yes', 'no', 'pm_open', 'pm_yes', 'pm_yes_risk', 'pm_no')"


def upgrade() -> None:
    op.create_table(
        "position_actions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("trade_id", sa.Integer(), nullable=True),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("position_id", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="CARD", nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("mark_price", sa.Numeric(28, 12), nullable=True),
        sa.Column("entry_price", sa.Numeric(28, 12), nullable=True),
        sa.Column("position_qty", sa.Numeric(28, 12), nullable=True),
        sa.Column("close_qty", sa.Numeric(28, 12), nullable=True),
        sa.Column("current_stop", sa.Numeric(28, 12), nullable=True),
        sa.Column("current_take", sa.Numeric(28, 12), nullable=True),
        sa.Column("new_level", sa.Numeric(28, 12), nullable=True),
        sa.Column("risk_before", sa.Numeric(20, 8), nullable=True),
        sa.Column("risk_after", sa.Numeric(20, 8), nullable=True),
        sa.Column("risk_before_r", sa.Numeric(28, 12), nullable=True),
        sa.Column("risk_after_r", sa.Numeric(28, 12), nullable=True),
        sa.Column("fee", sa.Numeric(20, 8), nullable=True),
        sa.Column("risk_increase", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("card_message_id", sa.BigInteger(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('MOVE_STOP', 'SET_TAKE', 'CLOSE_PARTIAL', 'CLOSE_FULL')",
            name=op.f("ck_position_actions_kind_known"),
        ),
        sa.CheckConstraint(
            "status IN ('CARD', 'DECLINED', 'EXPIRED', 'REFUSED', 'DRY_RUN', "
            "'SUBMITTED', 'DONE', 'FAILED')",
            name=op.f("ck_position_actions_status_known"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_position_actions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["trade_id"], ["trades.id"], name=op.f("fk_position_actions_trade_id_trades"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_position_actions")),
    )
    op.create_index(
        "ix_position_actions_user_created", "position_actions", ["user_id", "created_at"]
    )
    op.create_index(
        "ix_position_actions_user_symbol_side", "position_actions", ["user_id", "symbol", "side"]
    )

    op.add_column("trades", sa.Column("initial_stop_loss", sa.Numeric(28, 12), nullable=True))

    op.add_column(
        "execution_orders", sa.Column("position_action_id", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_execution_orders_position_action_id_position_actions"),
        "execution_orders", "position_actions", ["position_action_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_execution_orders_position_action_id", "execution_orders", ["position_action_id"]
    )

    op.alter_column(
        "execution_callbacks", "action",
        existing_type=sa.String(length=8), type_=sa.String(length=16), existing_nullable=False,
    )
    op.drop_constraint(
        op.f("ck_execution_callbacks_action_known"), "execution_callbacks", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_execution_callbacks_action_known"), "execution_callbacks", _NEW_ACTIONS
    )
    op.add_column(
        "execution_callbacks", sa.Column("position_action_id", sa.Integer(), nullable=True)
    )
    op.create_index(
        "ix_execution_callbacks_position_action", "execution_callbacks", ["position_action_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_execution_callbacks_position_action", table_name="execution_callbacks")
    op.drop_column("execution_callbacks", "position_action_id")
    # Старый CHECK и String(8) нажатий pm_* не допускают — они удаляются.
    op.execute("DELETE FROM execution_callbacks WHERE action LIKE 'pm\\_%'")
    op.drop_constraint(
        op.f("ck_execution_callbacks_action_known"), "execution_callbacks", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_execution_callbacks_action_known"), "execution_callbacks", _OLD_ACTIONS
    )
    op.alter_column(
        "execution_callbacks", "action",
        existing_type=sa.String(length=16), type_=sa.String(length=8), existing_nullable=False,
    )

    op.drop_index("ix_execution_orders_position_action_id", table_name="execution_orders")
    op.drop_constraint(
        op.f("fk_execution_orders_position_action_id_position_actions"),
        "execution_orders", type_="foreignkey",
    )
    op.drop_column("execution_orders", "position_action_id")

    op.drop_column("trades", "initial_stop_loss")

    op.drop_index("ix_position_actions_user_symbol_side", table_name="position_actions")
    op.drop_index("ix_position_actions_user_created", table_name="position_actions")
    op.drop_table("position_actions")
