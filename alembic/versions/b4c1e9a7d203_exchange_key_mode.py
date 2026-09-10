"""exchange key mode (LIVE/DEMO), active_exchange_mode setting

Этап 15.4в — два счёта BingX (реальный и демо) с переключением показа в
настройках. Ключ теперь принадлежит режиму (LIVE/DEMO), не только бирже:
UNIQUE(user_id, exchange, mode) вместо UNIQUE(user_id, exchange) — у
пользователя может быть по одной паре ключей на каждый режим.

Существующие строки exchange_credentials — единственный контур, что был
доступен до этого шага, — помечаются LIVE (server_default покрывает их
без отдельного UPDATE). UserSettings.active_exchange_mode по умолчанию
тоже LIVE (решение пользователя, раздел "Модель данных" ТЗ шага 15.4в).

Revision ID: b4c1e9a7d203
Revises: f3a92c7e1b0d
Create Date: 2026-09-10
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b4c1e9a7d203"
down_revision = "f3a92c7e1b0d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_user_exchange", "exchange_credentials", type_="unique")
    op.add_column(
        "exchange_credentials",
        sa.Column("mode", sa.String(length=8), nullable=False, server_default="LIVE"),
    )
    op.create_unique_constraint(
        "uq_user_exchange_mode", "exchange_credentials", ["user_id", "exchange", "mode"]
    )

    op.add_column(
        "user_settings",
        sa.Column(
            "active_exchange_mode", sa.String(length=8), nullable=False, server_default="LIVE"
        ),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "active_exchange_mode")

    op.drop_constraint("uq_user_exchange_mode", "exchange_credentials", type_="unique")
    # DEMO-ключи существуют только благодаря этой миграции — без них
    # восстановление UNIQUE(user_id, exchange) ниже упадёт на дублях
    # (пользователь с обеими парами даёт два ряда user_id+exchange).
    op.execute("DELETE FROM exchange_credentials WHERE mode = 'DEMO'")
    op.drop_column("exchange_credentials", "mode")
    op.create_unique_constraint(
        "uq_user_exchange", "exchange_credentials", ["user_id", "exchange"]
    )
