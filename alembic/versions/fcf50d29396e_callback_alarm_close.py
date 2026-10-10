"""M8: нажатия «🔴 Закрыть маркетом» под ALARM в журнале нажатий (A.1, 10.10.2026)

Только расширение CHECK execution_callbacks.action тремя значениями, без
данных и без новых колонок:
- to_close      — «🔴 Закрыть маркетом» под тревогой (показ подтверждения);
- to_close_yes  — «Да, закрыть» (аварийное закрытие через ядро открытия);
- to_close_no   — «Нет» на подтверждении.
Нажатие пишется до отправки на биржу (app/execution/callback_audit.py) —
без строки в журнале «Да, закрыть» отказывает.

downgrade: старый CHECK таких строк не допускает — они удаляются (как в M5
для to_*/ma_*), затем CHECK M5 — в хранимой форме PostgreSQL
(ARRAY[('x'::character varying)::text, …]), не через IN (...): IN Postgres
хранит как (ARRAY[…])::text[], а после dump → restore (копия репетиции)
выражение разбирается заново и становится поэлементным. Через IN схема после
downgrade ≠ baseline — первая репетиция M8 (10.10.2026) упала ровно на этом,
урок M5 (08.10) не был перенесён. Правило — CLAUDE.md, «Конвенции».

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

# CHECK M5 — в хранимой форме PostgreSQL (как _OLD_ACTIONS в M5): через IN (...)
# downgrade дал бы (ARRAY[…])::text[], а копия репетиции после restore хранит
# поэлементную запись — схема после downgrade ≠ baseline (10.10.2026).
_M5_ACTIONS = (
    "(action)::text = ANY (ARRAY["
    "('open'::character varying)::text, ('yes'::character varying)::text, "
    "('no'::character varying)::text, ('pm_open'::character varying)::text, "
    "('pm_yes'::character varying)::text, "
    "('pm_yes_risk'::character varying)::text, "
    "('pm_no'::character varying)::text, ('to_yes'::character varying)::text, "
    "('to_yes_warn'::character varying)::text, "
    "('to_no'::character varying)::text, ('to_cancel'::character varying)::text, "
    "('ma_confirm'::character varying)::text, "
    "('ma_cancel'::character varying)::text])"
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
