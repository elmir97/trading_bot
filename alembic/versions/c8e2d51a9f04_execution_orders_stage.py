"""execution_orders.stage, статус ERROR

Раздел 12а ТЗ, пакет C (полнота наблюдения). Сводка исполнения различала
отказы кода только по status: отказ на построении карточки и отказ на «Да»
были неотличимы, а сбои биржи вообще не записывались.

stage VARCHAR(16) NULL — "card" / "confirm" (app.trading.enums.ObservationStage).
NULL у всех существующих строк — это прежняя семантика «до карточки», а не
данные, которых не хватает; заполнять их задним числом не нужно (окно
сводки — скользящие 24 часа, старые строки уйдут из него за сутки).

Новый статус ERROR миграции не требует: status — VARCHAR(16) без CHECK
(ddace081d9fb), «ERROR» в него помещается.

downgrade: ERROR-строки старый код прочитать не сможет (Enum(native_enum=False)
поднимает LookupError на неизвестном значении, и падает ежедневная сводка),
поэтому перед drop_column они переводятся в REFUSED — строка-наблюдение
остаётся, error_code (имя класса исключения) сохраняется.

Revision ID: c8e2d51a9f04
Revises: 4f3837b79361
Create Date: 2026-09-22
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c8e2d51a9f04"
down_revision = "4f3837b79361"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("execution_orders", sa.Column("stage", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.execute("UPDATE execution_orders SET status='REFUSED' WHERE status='ERROR'")
    op.drop_column("execution_orders", "stage")
