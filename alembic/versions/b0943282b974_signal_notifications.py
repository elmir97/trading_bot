"""signal_notifications, execution_orders/trades.notification_id

Шаг 15.5.2а: уведомление о сетапе — неизменяемый снимок, адресуемый
notification_id (см. app/database/models/signal_notification.py). Строка
слота signals изменчива, путь исполнения больше на неё не опирается.

Бэкфилл: по одной строке уведомления на каждую строку signals из её
текущего состояния. notified_at — signals.notified_at (или updated_at, если
пуст: колонка nullable, хотя NULL-строк на проде нет). expires_at —
notified_at + TTL сигнала, тот же, что использует сканер
(Settings.setup_scanner_ttl_hours), а НЕ signals.expires_at: тот продлевается
каждым пересканом. trade_opened_at = NULL — все прошлые входы были сухим
прогоном, «сожжённые» слоты тем самым оживают.

sequence signal_notifications_id_seq ставится выше MAX(signals.id) ДО
бэкфилла: client_order_id теперь tj{notification_id}u{user}{E|S|T}, а
сухие прогоны после деплоя 15.5.2 уже могли записать tj{signal_id}u{user}E —
при notification_id == signal_id вход упёрся бы в ложный UNIQUE.

execution_orders.notification_id у существующих строк остаётся NULL — у них
есть signal_id. trades.notification_id — писатель появится в 15.5.4.
signals.trade_opened_at не удаляется (код перестаёт его читать и писать,
колонка уходит отдельной миграцией позже).

downgrade: перед удалением таблицы переносит отметки trade_opened_at из
уведомлений обратно в signals — иначе старый код после отката не увидит
входы, сделанные новым кодом, и гвард SIGNAL_ALREADY_USED их пропустит.
Строки execution_orders с client_order_id вида tj{notification_id}…
остаются: при долгом откате старый tj{signal_id}… может с ними совпасть.

Revision ID: b0943282b974
Revises: c8e2d51a9f04
Create Date: 2026-09-23
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from app.core.config import get_settings

revision = "b0943282b974"
down_revision = "c8e2d51a9f04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signal_notifications",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("signal_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(length=8), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("setup", sa.String(length=64), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=True),
        sa.Column("entry_low", sa.Numeric(precision=28, scale=12), nullable=True),
        sa.Column("entry_high", sa.Numeric(precision=28, scale=12), nullable=True),
        sa.Column("stop_loss", sa.Numeric(precision=28, scale=12), nullable=True),
        sa.Column("take_profit", sa.Numeric(precision=28, scale=12), nullable=True),
        sa.Column("confidence", sa.SmallInteger(), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trade_opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["signal_id"], ["signals.id"],
            name="fk_signal_notifications_signal_id_signals", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_signal_notifications_user_id_users", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_signal_notifications"),
    )
    op.create_index(
        "ix_signal_notifications_signal_id_notified_at",
        "signal_notifications",
        ["signal_id", "notified_at"],
    )
    op.create_index(
        "ix_signal_notifications_user_id_notified_at",
        "signal_notifications",
        ["user_id", "notified_at"],
    )

    # Все notification_id строго больше любого signal_id (см. docstring).
    op.execute(
        "SELECT setval('signal_notifications_id_seq', "
        "(SELECT COALESCE(MAX(id), 0) + 1 FROM signals), false)"
    )

    # int() — литерал попадает в SQL строкой (офлайн-режим тоже), значение
    # из настроек, не из пользовательского ввода.
    ttl_hours = int(get_settings().setup_scanner_ttl_hours)
    op.execute(
        "INSERT INTO signal_notifications "
        "(signal_id, user_id, level, fingerprint, setup, direction, "
        "entry_low, entry_high, stop_loss, take_profit, confidence, "
        "notified_at, expires_at) "
        "SELECT id, user_id, level, fingerprint, setup, direction, "
        "entry_low, entry_high, stop_loss, take_profit, confidence, "
        "COALESCE(notified_at, updated_at), "
        f"COALESCE(notified_at, updated_at) + make_interval(hours => {ttl_hours}) "
        "FROM signals ORDER BY id"
    )

    op.add_column(
        "execution_orders", sa.Column("notification_id", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        "fk_execution_orders_notification_id_signal_notifications",
        "execution_orders",
        "signal_notifications",
        ["notification_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_execution_orders_notification_id", "execution_orders", ["notification_id"]
    )

    op.add_column("trades", sa.Column("notification_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_trades_notification_id_signal_notifications",
        "trades",
        "signal_notifications",
        ["notification_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE signals s SET trade_opened_at = sub.t "
        "FROM (SELECT signal_id, MAX(trade_opened_at) AS t FROM signal_notifications "
        "WHERE trade_opened_at IS NOT NULL GROUP BY signal_id) sub "
        "WHERE s.id = sub.signal_id "
        "AND (s.trade_opened_at IS NULL OR s.trade_opened_at < sub.t)"
    )

    op.drop_constraint(
        "fk_trades_notification_id_signal_notifications", "trades", type_="foreignkey"
    )
    op.drop_column("trades", "notification_id")

    op.drop_index("ix_execution_orders_notification_id", table_name="execution_orders")
    op.drop_constraint(
        "fk_execution_orders_notification_id_signal_notifications",
        "execution_orders",
        type_="foreignkey",
    )
    op.drop_column("execution_orders", "notification_id")

    op.drop_index(
        "ix_signal_notifications_user_id_notified_at", table_name="signal_notifications"
    )
    op.drop_index(
        "ix_signal_notifications_signal_id_notified_at", table_name="signal_notifications"
    )
    op.drop_table("signal_notifications")
