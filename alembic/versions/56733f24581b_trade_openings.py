"""M5: открытие сделки из бота (trade_openings и связи)

- trade_openings — снимок карточки «Открыть на бирже» и состояние открытия
  (docs/open-trade-plan.md, §3–4). Новая таблица, пустая.
- execution_orders.trade_opening_id — FK на trade_openings (SET NULL) + индекс.
  Nullable, без бэкфилла.
- execution_callbacks: CHECK расширен на to_yes/to_yes_warn/to_no/to_cancel
  (кнопки чата) и ma_confirm/ma_cancel (Mini App — сразу, без второй
  миграции); trade_opening_id (без FK, как position_action_id) + индекс.
- user_settings.margin_type_default — VARCHAR(8), NULL = изолированная.
- trades.source 'BOT', execution_orders.status 'WORKING', order_type 'LIMIT' —
  строки в существующих VARCHAR без CHECK, DDL не нужен.

Репетиция: --checksum --expect-columns trade_openings.id,
execution_orders.trade_opening_id,execution_callbacks.trade_opening_id,
user_settings.margin_type_default --expect-null execution_orders.trade_opening_id,
execution_callbacks.trade_opening_id,user_settings.margin_type_default
--allow-count-change trade_openings.

downgrade: нажатия to_*/ma_* удаляются (старый CHECK их не допускает), сделки
source='BOT' становятся 'IMPORTED' (старый код не знает BOT и упал бы на
чтении; IMPORTED со счётом и positionId сверяется с биржей, вход по orderId
из execution_orders импорт по-прежнему пропускает). Без открытий (до деплоя
ядра) данные не меняются.

Revision ID: 56733f24581b
Revises: 3f7a9c1e5d20
Create Date: 2026-10-05
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "56733f24581b"
down_revision = "3f7a9c1e5d20"
branch_labels = None
depends_on = None

_OLD_ACTIONS = "action IN ('open', 'yes', 'no', 'pm_open', 'pm_yes', 'pm_yes_risk', 'pm_no')"
_NEW_ACTIONS = (
    "action IN ('open', 'yes', 'no', 'pm_open', 'pm_yes', 'pm_yes_risk', 'pm_no', "
    "'to_yes', 'to_yes_warn', 'to_no', 'to_cancel', 'ma_confirm', 'ma_cancel')"
)
_STATUSES = (
    "status IN ('CARD', 'DECLINED', 'EXPIRED_CARD', 'REFUSED', 'DRY_RUN', 'CONFIRMED', "
    "'SUBMITTING', 'UNKNOWN', 'WORKING', 'FILLED', 'PROTECTED', 'DONE', 'REJECTED', "
    "'NOT_PLACED', 'CANCELLED', 'EXPIRED', 'EMERGENCY_CLOSED', 'ALARM')"
)
_ACTIVE = (
    "status IN ('CONFIRMED', 'SUBMITTING', 'UNKNOWN', 'WORKING', 'FILLED', 'PROTECTED', "
    "'ALARM')"
)


def upgrade() -> None:
    op.create_table(
        "trade_openings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("account_mode", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="CARD", nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("entry_type", sa.String(length=8), nullable=False),
        sa.Column("limit_price", sa.Numeric(28, 12), nullable=True),
        sa.Column("stop_loss", sa.Numeric(28, 12), nullable=False),
        sa.Column("take_profit", sa.Numeric(28, 12), nullable=True),
        sa.Column("risk_percent", sa.Numeric(12, 4), nullable=False),
        sa.Column("leverage", sa.Integer(), nullable=False),
        sa.Column("margin_type", sa.String(length=8), nullable=False),
        sa.Column("expiry_minutes", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("card_price", sa.Numeric(28, 12), nullable=True),
        sa.Column("equity", sa.Numeric(20, 8), nullable=True),
        sa.Column("available", sa.Numeric(20, 8), nullable=True),
        sa.Column("quantity", sa.Numeric(28, 12), nullable=True),
        sa.Column("risk_usd", sa.Numeric(20, 8), nullable=True),
        sa.Column("fee_estimate", sa.Numeric(20, 8), nullable=True),
        sa.Column("margin", sa.Numeric(20, 8), nullable=True),
        sa.Column("rr", sa.Numeric(28, 12), nullable=True),
        sa.Column("liq_estimate", sa.Numeric(28, 12), nullable=True),
        sa.Column("violations", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "warnings_accepted", sa.Boolean(), server_default="false", nullable=False
        ),
        sa.Column("entry_order_id", sa.String(length=64), nullable=True),
        sa.Column("position_id", sa.String(length=64), nullable=True),
        sa.Column("filled_qty", sa.Numeric(28, 12), nullable=True),
        sa.Column("avg_price", sa.Numeric(28, 12), nullable=True),
        sa.Column("entry_fee", sa.Numeric(20, 8), nullable=True),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trade_id", sa.Integer(), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("card_message_id", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_STATUSES, name=op.f("ck_trade_openings_status_known")),
        sa.CheckConstraint(
            "entry_type IN ('MARKET', 'LIMIT')", name=op.f("ck_trade_openings_entry_type_known")
        ),
        sa.CheckConstraint(
            "source IN ('wizard', 'miniapp')", name=op.f("ck_trade_openings_source_known")
        ),
        sa.CheckConstraint(
            "account_mode IN ('LIVE', 'DEMO')", name=op.f("ck_trade_openings_account_mode_known")
        ),
        sa.CheckConstraint(
            "entry_type = 'MARKET' OR limit_price IS NOT NULL",
            name=op.f("ck_trade_openings_limit_has_price"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_trade_openings_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["trade_id"], ["trades.id"], name=op.f("fk_trade_openings_trade_id_trades"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trade_openings")),
    )
    op.create_index(
        "ix_trade_openings_user_created", "trade_openings", ["user_id", "created_at"]
    )
    op.create_index(
        "ix_trade_openings_active", "trade_openings", ["status"],
        postgresql_where=sa.text(_ACTIVE),
    )

    op.add_column(
        "execution_orders", sa.Column("trade_opening_id", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_execution_orders_trade_opening_id_trade_openings"),
        "execution_orders", "trade_openings", ["trade_opening_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_execution_orders_trade_opening_id", "execution_orders", ["trade_opening_id"]
    )

    op.drop_constraint(
        op.f("ck_execution_callbacks_action_known"), "execution_callbacks", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_execution_callbacks_action_known"), "execution_callbacks", _NEW_ACTIONS
    )
    op.add_column(
        "execution_callbacks", sa.Column("trade_opening_id", sa.Integer(), nullable=True)
    )
    op.create_index(
        "ix_execution_callbacks_trade_opening", "execution_callbacks", ["trade_opening_id"]
    )

    op.add_column(
        "user_settings", sa.Column("margin_type_default", sa.String(length=8), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("user_settings", "margin_type_default")

    op.drop_index("ix_execution_callbacks_trade_opening", table_name="execution_callbacks")
    op.drop_column("execution_callbacks", "trade_opening_id")
    # Старый CHECK нажатий to_*/ma_* не допускает — они удаляются.
    op.execute(
        "DELETE FROM execution_callbacks WHERE action LIKE 'to\\_%' OR action LIKE 'ma\\_%'"
    )
    op.drop_constraint(
        op.f("ck_execution_callbacks_action_known"), "execution_callbacks", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_execution_callbacks_action_known"), "execution_callbacks", _OLD_ACTIONS
    )

    op.drop_index("ix_execution_orders_trade_opening_id", table_name="execution_orders")
    op.drop_constraint(
        op.f("fk_execution_orders_trade_opening_id_trade_openings"),
        "execution_orders", type_="foreignkey",
    )
    op.drop_column("execution_orders", "trade_opening_id")

    # Старый код не знает source='BOT' (SQLAlchemy Enum упал бы на чтении).
    op.execute("UPDATE trades SET source = 'IMPORTED' WHERE source = 'BOT'")

    op.drop_index("ix_trade_openings_active", table_name="trade_openings")
    op.drop_index("ix_trade_openings_user_created", table_name="trade_openings")
    op.drop_table("trade_openings")
