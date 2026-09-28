"""target_source, exchange_realized_pnl, risk_reward_net

28.09, блоки B, C и A (часть в БД), одной миграцией:

- signals.target_source, signal_notifications.target_source — VARCHAR(16)
  NULL: источник цели READY-сигнала (LEVEL / FORMULA_2R). Снимок уведомления
  несёт его на карточку. У существующих строк NULL — источник задним числом
  неизвестен, старые уведомления строку «Цель: …» не показывают.
- trade_fills.exchange_realized_pnl — NUMERIC(20, 8) NULL: profit биржи по
  закрывающему ордеру (allOrders), пишет reconciler. По ним при полном
  закрытии — сверка PnL журнала с биржей (PNL_MISMATCH).
- execution_orders.risk_reward_net — NUMERIC(28, 12) NULL: RR с taker-
  комиссией, как на карточке. risk_reward остаётся без комиссии.

Все четыре — nullable без бэкфилла и без CHECK: kind/target_source —
VARCHAR, как остальные Enum(native_enum=False) проекта.

downgrade: колонки удаляются, значения теряются — старый код их не читает.
События PNL_MISMATCH (reconciliation_events.kind — VARCHAR(32)) остаются;
старый код падает на незнакомом kind (LookupError) — переводятся в
AMBIGUOUS, ближайшее «расхождение без однозначного факта».

Revision ID: 19c5c0deedca
Revises: df411b3ca043
Create Date: 2026-09-28
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "19c5c0deedca"
down_revision = "df411b3ca043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("signals", sa.Column("target_source", sa.String(length=16), nullable=True))
    op.add_column(
        "signal_notifications",
        sa.Column("target_source", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "trade_fills",
        sa.Column("exchange_realized_pnl", sa.Numeric(20, 8), nullable=True),
    )
    op.add_column(
        "execution_orders",
        sa.Column("risk_reward_net", sa.Numeric(28, 12), nullable=True),
    )


def downgrade() -> None:
    op.execute(
        "UPDATE reconciliation_events SET kind='AMBIGUOUS' WHERE kind='PNL_MISMATCH'"
    )
    op.drop_column("execution_orders", "risk_reward_net")
    op.drop_column("trade_fills", "exchange_realized_pnl")
    op.drop_column("signal_notifications", "target_source")
    op.drop_column("signals", "target_source")
