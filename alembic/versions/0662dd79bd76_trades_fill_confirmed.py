"""trades.fill_confirmed, уникальная сделка на уведомление

Шаг 15.5.4: сделка бота пишется в журнал после read-back при любом исходе
входа, кроме отказа. Если исполнение не подтверждено (read-back не прочитал
цену, UNKNOWN), сделка — предварительная: цена плановая, fill_confirmed =
false; reconciler 15.6 будет искать такие строки. Ложная блокировка символа
до сверки безопаснее второй позиции: гварды POSITION_EXISTS / MAX_POSITIONS /
MAX_TOTAL_RISK считают открытые позиции по trades.

fill_confirmed BOOLEAN NOT NULL DEFAULT true — существующие (ручные и
импортированные) сделки подтверждены по определению.

uq_trades_notification_id — частичный уникальный индекс: одна сделка на одно
уведомление, второй уровень идемпотентности под execution_orders.trade_id
(параллельная запись хендлера и будущего reconciler). Сделки без уведомления
(ручные, импорт) не ограничиваются.

downgrade: признак неподтверждённости теряется — старый код его не читает;
при долгом откате reconciler 15.6 такие сделки не найдёт.

Revision ID: 0662dd79bd76
Revises: b0943282b974
Create Date: 2026-09-24
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0662dd79bd76"
down_revision = "b0943282b974"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trades",
        sa.Column(
            "fill_confirmed", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
    )
    op.create_index(
        "uq_trades_notification_id",
        "trades",
        ["notification_id"],
        unique=True,
        postgresql_where=sa.text("notification_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_trades_notification_id", table_name="trades")
    op.drop_column("trades", "fill_confirmed")
