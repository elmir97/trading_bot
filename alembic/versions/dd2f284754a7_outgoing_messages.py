"""M6: журнал исходящих итоговых сообщений (очередь A.2, 09.10.2026)

- outgoing_messages — одно сообщение в чате (chat_id, message_id) с итогом
  открытия сделки или действия с позицией: текст, вид, связи с открытием /
  сделкой / действием (SET NULL), время отправки и правок (edits JSONB).
  UNIQUE (chat_id, message_id), индекс по trade_opening_id. Новая таблица,
  пустая; данные не меняются.

Репетиция: --checksum --allow-count-change outgoing_messages.

downgrade: таблица удаляется вместе со строками (теряется только журнал
сообщений; бот работает и без него).

Revision ID: dd2f284754a7
Revises: 56733f24581b
Create Date: 2026-10-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "dd2f284754a7"
down_revision = "56733f24581b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outgoing_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("trade_opening_id", sa.Integer(), nullable=True),
        sa.Column("trade_id", sa.Integer(), nullable=True),
        sa.Column("position_action_id", sa.Integer(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "sent_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "edits", postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_outgoing_messages_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["trade_opening_id"], ["trade_openings.id"],
            name=op.f("fk_outgoing_messages_trade_opening_id_trade_openings"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["trade_id"], ["trades.id"], name=op.f("fk_outgoing_messages_trade_id_trades"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["position_action_id"], ["position_actions.id"],
            name=op.f("fk_outgoing_messages_position_action_id_position_actions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outgoing_messages")),
        sa.UniqueConstraint(
            "chat_id", "message_id", name=op.f("uq_outgoing_messages_chat_message")
        ),
    )
    op.create_index(
        "ix_outgoing_messages_trade_opening_id", "outgoing_messages", ["trade_opening_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_outgoing_messages_trade_opening_id", table_name="outgoing_messages")
    op.drop_table("outgoing_messages")
