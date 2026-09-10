"""execution_orders observation columns, user_settings digest date

Этап 15.4, раздел 12а — ежедневная сводка исполнения. Наблюдение
переиспользует execution_orders вместо новой таблицы: строка теперь может
означать не только "ордер, который ушёл бы на биржу" (DRY_RUN и позже
реальные статусы), но и "карточка подтверждения показана и чем закончилась"
(DECLINED/EXPIRED) или "во входе отказал гвард ещё до показа карточки"
(REFUSED, error_code/error_message переиспользуются под finding_code гварда
и его сообщение — то же поле, что и для будущих отказов биржи).

Для DECLINED/EXPIRED/REFUSED заранее не существует ни идемпотентного
отправления на биржу (client_order_id теряет смысл — нечего отправлять и
не о чем сверяться), ни объёма (REFUSED в общем случае наступает раньше
sizing.calculate_size), поэтому оба поля становятся nullable.

Revision ID: f3a92c7e1b0d
Revises: ddace081d9fb
Create Date: 2026-09-10
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "f3a92c7e1b0d"
down_revision = "ddace081d9fb"
branch_labels = None
depends_on = None

NUMERIC = sa.Numeric(precision=28, scale=12)


def upgrade() -> None:
    op.alter_column("execution_orders", "client_order_id", nullable=True)
    op.alter_column("execution_orders", "quantity", nullable=True)

    op.add_column("execution_orders", sa.Column("price_drift_percent", NUMERIC, nullable=True))
    op.add_column("execution_orders", sa.Column("notional", NUMERIC, nullable=True))
    op.add_column("execution_orders", sa.Column("margin", NUMERIC, nullable=True))
    op.add_column("execution_orders", sa.Column("leverage", sa.Integer(), nullable=True))
    op.add_column("execution_orders", sa.Column("risk_amount", NUMERIC, nullable=True))
    op.add_column("execution_orders", sa.Column("risk_percent", NUMERIC, nullable=True))
    op.add_column("execution_orders", sa.Column("risk_reward", NUMERIC, nullable=True))

    op.add_column(
        "user_settings",
        sa.Column("execution_digest_last_sent_date", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    # Строки-наблюдения (REFUSED/DECLINED/EXPIRED) и сухие прогоны DRY_RUN
    # существуют только благодаря этой миграции (nullable client_order_id/
    # quantity, снимок расчёта раздела 12а) — без них ALTER ... SET NOT NULL
    # ниже падает на NULL-значениях. Откат обязан проходить всегда, поэтому
    # сперва вычищаем то, что этот шаг сделал возможным, а не отказываем.
    op.execute(
        "DELETE FROM execution_orders WHERE status IN "
        "('REFUSED', 'DECLINED', 'EXPIRED', 'DRY_RUN')"
    )

    op.drop_column("user_settings", "execution_digest_last_sent_date")

    op.drop_column("execution_orders", "risk_reward")
    op.drop_column("execution_orders", "risk_percent")
    op.drop_column("execution_orders", "risk_amount")
    op.drop_column("execution_orders", "leverage")
    op.drop_column("execution_orders", "margin")
    op.drop_column("execution_orders", "notional")
    op.drop_column("execution_orders", "price_drift_percent")

    op.alter_column("execution_orders", "quantity", nullable=False)
    op.alter_column("execution_orders", "client_order_id", nullable=False)
