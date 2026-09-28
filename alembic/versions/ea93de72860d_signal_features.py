"""Признаки READY-сигнала в слоте и снимке уведомления

28.09, отчёт исходов сигналов: чтобы резать исходы по признакам, а не
восстанавливать их задним числом по свечам, сканер пишет их в момент
сигнала. Одинаковые колонки в signals (слот) и signal_notifications
(снимок копирует слот — SignalNotification.snapshot_of):

- atr — NUMERIC(28, 12): ATR(14) на момент сигнала, в цене инструмента.
- volume_ratio_last — NUMERIC(28, 12): отношение объёма последней свечи
  к среднему за 20.
- stop_pct — NUMERIC(28, 12): |вход − стоп| / вход × 100.
- breakout_volume_ratio — NUMERIC(28, 12): то же отношение у пробойной
  свечи (только «Пробой с ретестом»).
- breakout_at — TIMESTAMPTZ: open_time пробойной свечи (только «Пробой с
  ретестом»); ключ среза «один сигнал на пробой».
- ema50_distance_atr — NUMERIC(28, 12): |цена − EMA50| / ATR (только
  «Откат к EMA50»).

Все — nullable без бэкфилла: у FORMING и у строк до миграции NULL.
В отпечаток (fingerprint) не входят.

downgrade: колонки удаляются, значения теряются — старый код их не читает.

Revision ID: ea93de72860d
Revises: 7b4e2c9a1f35
Create Date: 2026-09-28
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "ea93de72860d"
down_revision = "7b4e2c9a1f35"
branch_labels = None
depends_on = None

_TABLES = ("signals", "signal_notifications")


def _columns() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("atr", sa.Numeric(28, 12), nullable=True),
        sa.Column("volume_ratio_last", sa.Numeric(28, 12), nullable=True),
        sa.Column("stop_pct", sa.Numeric(28, 12), nullable=True),
        sa.Column("breakout_volume_ratio", sa.Numeric(28, 12), nullable=True),
        sa.Column("breakout_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ema50_distance_atr", sa.Numeric(28, 12), nullable=True),
    ]


def upgrade() -> None:
    for table in _TABLES:
        for column in _columns():
            op.add_column(table, column)


def downgrade() -> None:
    for table in reversed(_TABLES):
        for column in reversed(_columns()):
            op.drop_column(table, column.name)
