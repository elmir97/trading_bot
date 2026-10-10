"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    # Прежний CHECK с IN (...) возвращать в хранимой форме Postgres —
    # (col)::text = ANY (ARRAY[('x'::character varying)::text, ...]), как в M5/M8;
    # через IN схема после downgrade != baseline репетиции (CLAUDE.md, «Конвенции»).
    ${downgrades if downgrades else "pass"}
