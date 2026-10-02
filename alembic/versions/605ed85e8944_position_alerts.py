"""M2: уведомления о приближении к SL/TP (этап 5) и код кнопки в журнале нажатий

- position_alerts — отметки «уведомили о приближении к уровню», дедуп по
  уровню: UNIQUE (user_id, symbol, side, kind, level_price). Новая таблица,
  пустая.
- user_settings.sl_alert_percent / tp_alert_percent — порог в процентах пути
  до уровня. Nullable, без бэкфилла: NULL — по умолчанию 80. Переключатели
  sl_approaching / tp_approaching — ключи JSONB notifications, данные не
  меняются: без нового ключа код читает старый tp_sl_approaching.
- execution_callbacks.raw_data — callback_data кнопки (до 64 символов).
  Nullable, без бэкфилла: старые нажатия — NULL.

trades.tp_approach_notified_at / sl_approach_notified_at больше не пишутся;
удаляются вместе с таблицами сигналов отдельной миграцией после дампа.

Репетиция: --checksum --expect-null user_settings.sl_alert_percent,
user_settings.tp_alert_percent,execution_callbacks.raw_data
--allow-count-change position_alerts.

downgrade: всё обратно; position_alerts удаляется вместе со строками
(повторные уведомления по уже уведомлённым уровням — допустимо), raw_data и
пороги теряются.

Revision ID: 605ed85e8944
Revises: 5951467e6d9a
Create Date: 2026-10-02
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "605ed85e8944"
down_revision = "5951467e6d9a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "position_alerts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("kind", sa.String(length=2), nullable=False),
        sa.Column("level_price", sa.Numeric(28, 12), nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('SL', 'TP')", name=op.f("ck_position_alerts_kind_known")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_position_alerts_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_position_alerts")),
        sa.UniqueConstraint(
            "user_id", "symbol", "side", "kind", "level_price",
            name=op.f("uq_position_alerts_key"),
        ),
    )

    op.add_column("user_settings", sa.Column("sl_alert_percent", sa.SmallInteger(), nullable=True))
    op.add_column("user_settings", sa.Column("tp_alert_percent", sa.SmallInteger(), nullable=True))

    op.add_column(
        "execution_callbacks", sa.Column("raw_data", sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("execution_callbacks", "raw_data")
    op.drop_column("user_settings", "tp_alert_percent")
    op.drop_column("user_settings", "sl_alert_percent")
    op.drop_table("position_alerts")
