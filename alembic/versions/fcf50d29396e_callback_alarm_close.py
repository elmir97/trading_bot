"""M8: нажатия «🔴 Закрыть маркетом» под ALARM в журнале нажатий (A.1, 10.10.2026)

Только расширение CHECK execution_callbacks.action тремя значениями, без
данных и без новых колонок:
- to_close      — «🔴 Закрыть маркетом» под тревогой (показ подтверждения);
- to_close_yes  — «Да, закрыть» (аварийное закрытие через ядро открытия);
- to_close_no   — «Нет» на подтверждении.
Нажатие пишется до отправки на биржу (app/execution/callback_audit.py) —
без строки в журнале «Да, закрыть» отказывает.

downgrade: старый CHECK таких строк не допускает — они удаляются (как в M5
для to_*/ma_*), затем CHECK M5 тем же выражением, что создала M5: PostgreSQL
сохраняет его в той же форме, что сейчас на проде (схема после downgrade =
baseline репетиции).

Репетиция: --checksum, без --expect-columns и --allow-count-change (новых
таблиц и колонок нет); в «Схема baseline → upgrade 1» — только выражение
ck_execution_callbacks_action_known.

Revision ID: fcf50d29396e
Revises: e7c41a9b3d52
Create Date: 2026-10-10
"""

from __future__ import annotations

from alembic import op

revision = "fcf50d29396e"
down_revision = "e7c41a9b3d52"
branch_labels = None
depends_on = None

_M5_ACTIONS = (
    "action IN ('open', 'yes', 'no', 'pm_open', 'pm_yes', 'pm_yes_risk', 'pm_no', "
    "'to_yes', 'to_yes_warn', 'to_no', 'to_cancel', 'ma_confirm', 'ma_cancel')"
)
_NEW_ACTIONS = (
    "action IN ('open', 'yes', 'no', 'pm_open', 'pm_yes', 'pm_yes_risk', 'pm_no', "
    "'to_yes', 'to_yes_warn', 'to_no', 'to_cancel', 'ma_confirm', 'ma_cancel', "
    "'to_close', 'to_close_yes', 'to_close_no')"
)


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_execution_callbacks_action_known"), "execution_callbacks", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_execution_callbacks_action_known"), "execution_callbacks", _NEW_ACTIONS
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM execution_callbacks "
        "WHERE action IN ('to_close', 'to_close_yes', 'to_close_no')"
    )
    op.drop_constraint(
        op.f("ck_execution_callbacks_action_known"), "execution_callbacks", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_execution_callbacks_action_known"), "execution_callbacks", _M5_ACTIONS
    )
