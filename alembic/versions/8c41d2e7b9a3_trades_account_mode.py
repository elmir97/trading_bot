"""M3: счёт сделки (trades.account_mode) — лимиты убытка по своему счёту

- trades.account_mode — VARCHAR(8), nullable, CHECK: NULL | 'LIVE' | 'DEMO'.
  DEMO/LIVE — сделка с биржи, NULL — ручная запись журнала без счёта.

Бэкфилл (решение владельца 03.10.2026):
- source IN ('IMPORTED', 'SIGNAL_EXECUTION') → 'DEMO'. Боевой режим не
  включался ни разу: EXEC_ALLOW_LIVE_MODE_ORDERS=false и BINGX_TRADING_MODE=demo
  с первого дня исполнения, боевой счёт пуст (0 USDT); вход бота шёл только на
  VST-хост, импорт и «В журнал» — по счёту из настроек, всё время DEMO
  (проверка перед деплоем: user_settings.active_exchange_mode = 'DEMO');
- source = 'MANUAL' → NULL.

Репетиция: --checksum --expect-columns trades.account_mode. Без
--expect-null (колонка заполняется бэкфиллом) и без --allow-data-change:
downgrade удаляет колонку целиком, md5 считается по колонкам baseline.

downgrade: колонка удаляется вместе с CHECK; счёт сделок теряется.

Revision ID: 8c41d2e7b9a3
Revises: 605ed85e8944
Create Date: 2026-10-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "8c41d2e7b9a3"
down_revision = "605ed85e8944"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trades", sa.Column("account_mode", sa.String(length=8), nullable=True))
    op.create_check_constraint(
        op.f("ck_trades_account_mode_known"),
        "trades",
        "account_mode IS NULL OR account_mode IN ('LIVE', 'DEMO')",
    )
    op.execute(
        "UPDATE trades SET account_mode = 'DEMO' "
        "WHERE source IN ('IMPORTED', 'SIGNAL_EXECUTION')"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_trades_account_mode_known"), "trades", type_="check")
    op.drop_column("trades", "account_mode")
