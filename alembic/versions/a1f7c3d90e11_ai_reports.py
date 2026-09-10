"""ai_reports table

Revision ID: a1f7c3d90e11
Revises: cb2347fb7871
Create Date: 2026-09-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a1f7c3d90e11"
down_revision = "cb2347fb7871"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_reports",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("facts_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("report_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ai_reports_user_fingerprint", "ai_reports", ["user_id", "fingerprint"], unique=True
    )
    op.create_index("ix_ai_reports_user_created", "ai_reports", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_ai_reports_user_created", table_name="ai_reports")
    op.drop_index("ix_ai_reports_user_fingerprint", table_name="ai_reports")
    op.drop_table("ai_reports")
