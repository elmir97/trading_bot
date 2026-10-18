"""M9: счёт биржи у действий с позицией и у ордеров (B.9, 19.10.2026)

- position_actions.account_mode — VARCHAR(8) NOT NULL, CHECK IN ('LIVE', 'DEMO'):
  каким счётом шло действие этапа 4. Восстановление после рестарта (B.9,
  app/execution/action_recovery.py) проверяет ордера по cid клиентом ЭТОГО
  счёта: 109421 «order not exist» от клиента другого режима — не доказательство.
- execution_orders.account_mode — то же для каждой строки ордера (у открытий
  счёт был только в trade_openings, у входов по сигналу и действий — нигде).

Данные (решение владельца 19.10): существующие строки — DEMO. Live не
включался ни разу (на 18.10 на проде: trade_openings 35 — все DEMO, trades
20 — все DEMO, live-preflight не проводился). Строки ордеров открытий берут
счёт своего открытия, остальные — DEMO. Проверка, а не вера: есть сделка или
открытие с LIVE — upgrade падает (одна транзакция, alembic/env.py), разметка
DEMO была бы ложью.

Репетиция: --checksum --expect-columns position_actions.account_mode,
execution_orders.account_mode; без --expect-null (колонки заполняются) и без
--allow-data-change (прежние колонки не меняются). В выводе upgrade —
«M9: счёт размечен» с числами строк.

downgrade: CHECK и колонки удаляются — прежний код без них работает.

Revision ID: 919ae8ad384e
Revises: fcf50d29396e
Create Date: 2026-10-19
"""

from __future__ import annotations

import logging

import sqlalchemy as sa

from alembic import context, op

revision = "919ae8ad384e"
down_revision = "fcf50d29396e"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

_KNOWN = "account_mode IN ('LIVE', 'DEMO')"
_TABLES = ("position_actions", "execution_orders")


# Проверка — в SQL, а не в Python: работает и в офлайн-режиме (--sql, показ
# владельцу перед применением). Текст исключения — ASCII.
_NO_LIVE = (
    "DO $$ BEGIN "
    "IF EXISTS (SELECT 1 FROM trades WHERE account_mode = 'LIVE') "
    "OR EXISTS (SELECT 1 FROM trade_openings WHERE account_mode = 'LIVE') THEN "
    "RAISE EXCEPTION 'M9: LIVE rows in trades/trade_openings - DEMO backfill would be wrong'; "
    "END IF; END $$"
)
_FILL = (
    ("position_actions DEMO", "UPDATE position_actions SET account_mode = 'DEMO'"),
    ("execution_orders по открытию",
     "UPDATE execution_orders e SET account_mode = o.account_mode"
     " FROM trade_openings o WHERE o.id = e.trade_opening_id"),
    ("execution_orders DEMO",
     "UPDATE execution_orders SET account_mode = 'DEMO' WHERE account_mode IS NULL"),
)


def upgrade() -> None:
    op.execute(_NO_LIVE)
    for table in _TABLES:
        op.add_column(table, sa.Column("account_mode", sa.String(length=8), nullable=True))

    if context.is_offline_mode():
        for _, sql in _FILL:
            op.execute(sql)
    else:
        bind = op.get_bind()
        counts = [f"{name} {bind.execute(sa.text(sql)).rowcount}" for name, sql in _FILL]
        log.info("M9: счёт размечен — %s", ", ".join(counts))

    for table in _TABLES:
        op.alter_column(table, "account_mode", existing_type=sa.String(length=8),
                        nullable=False)
        op.create_check_constraint(op.f(f"ck_{table}_account_mode_known"), table, _KNOWN)


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_constraint(op.f(f"ck_{table}_account_mode_known"), table, type_="check")
        op.drop_column(table, "account_mode")
