"""execution_orders.card_price, card_quantity

Цена и объём на момент показа карточки подтверждения. На «Да» evaluate()
перезапрашивает цену и пересчитывает объём (раздел 5 ТЗ), и в price/quantity
ENTRY-строки ложатся уже они. Карточка жила только в памяти процесса
(_ConfirmationState), поэтому «карточка → на «Да»» (дрейф до подтверждения,
пересчёт объёма) задним числом было не восстановить: сделка #3 27.09 —
карточка 14.393, на «Да» 14.398, в строке только второе.

Обе NUMERIC(28, 12) NULL, заполняются только в ENTRY-строке входа (DRY_RUN
и реальной отправки). NULL у всех существующих строк: карточку их входов
восстановить неоткуда, бэкфилла нет.

downgrade: колонки удаляются, значения теряются — старый код их не читает.

Revision ID: 407583974eb1
Revises: 0662dd79bd76
Create Date: 2026-09-27
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "407583974eb1"
down_revision = "0662dd79bd76"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "execution_orders", sa.Column("card_price", sa.Numeric(28, 12), nullable=True)
    )
    op.add_column(
        "execution_orders", sa.Column("card_quantity", sa.Numeric(28, 12), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("execution_orders", "card_quantity")
    op.drop_column("execution_orders", "card_price")
