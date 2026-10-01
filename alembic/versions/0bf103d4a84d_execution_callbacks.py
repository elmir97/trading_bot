"""execution_callbacks — журнал нажатий кнопок исполнения

Префлайт 15.7: нажатия exn:open / exn:yes / exn:no пишутся строкой —
user_id (FK, CASCADE), action (CHECK open/yes/no), notification_id (без FK,
nullable: битые и чужие кнопки тоже пишутся), chat_id, message_id,
callback_query_id, created_at. Пишет app/execution/callback_audit.py из
отдельной сессии.

Новая таблица, без бэкфилла; существующие таблицы не меняются. Репетиция:
--allow-count-change execution_callbacks (в baseline таблицы нет).

downgrade: таблица удаляется вместе с журналом нажатий — старый код её не
читает.

Revision ID: 0bf103d4a84d
Revises: ea93de72860d
Create Date: 2026-10-01
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0bf103d4a84d"
down_revision = "ea93de72860d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "execution_callbacks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=8), nullable=False),
        sa.Column("notification_id", sa.Integer(), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("callback_query_id", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "action IN ('open', 'yes', 'no')", name=op.f("ck_execution_callbacks_action_known")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_execution_callbacks_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execution_callbacks")),
    )
    op.create_index(
        "ix_execution_callbacks_user_created", "execution_callbacks", ["user_id", "created_at"]
    )
    op.create_index(
        "ix_execution_callbacks_notification", "execution_callbacks", ["notification_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_execution_callbacks_notification", table_name="execution_callbacks")
    op.drop_index("ix_execution_callbacks_user_created", table_name="execution_callbacks")
    op.drop_table("execution_callbacks")
