"""K8: preserve mixed RR including NULL; new rows have an explicit boundary.

Rollback requires the verified archive procedure in scripts.k8_rollback.
Normal upgrade is entirely SQL and never reads an external archive.
"""

import sqlalchemy as sa

from alembic import op

revision = "c8a030202610"
down_revision = "919ae8ad384e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("LOCK TABLE trades, trade_fills IN ACCESS EXCLUSIVE MODE")
    op.add_column("trades", sa.Column("legacy_risk_reward", sa.Numeric(12, 4)))
    op.add_column("trades", sa.Column(
        "k8_legacy_row", sa.Boolean(), nullable=False, server_default=sa.false()
    ))
    op.execute("UPDATE trades SET legacy_risk_reward = risk_reward, k8_legacy_row = true")
    op.execute("""
        UPDATE trades t SET risk_reward = NULL
        WHERE status <> 'OPEN' OR exit_price IS NOT NULL OR NOT fill_confirmed
           OR source NOT IN ('MANUAL', 'BOT', 'SIGNAL_EXECUTION')
           OR (SELECT count(*) FROM trade_fills f
               WHERE f.trade_id = t.id AND f.fill_side = 'ENTRY') <> 1
           OR EXISTS (SELECT 1 FROM trade_fills f
                      WHERE f.trade_id = t.id AND f.fill_side = 'EXIT')
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
          IF current_setting('k8.rollback_verified', true) IS DISTINCT FROM 'yes' THEN
            RAISE EXCEPTION 'K8: use scripts.k8_rollback downgrade with verified archive';
          END IF;
        END $$
    """)
    op.execute("LOCK TABLE trades, trade_fills IN ACCESS EXCLUSIVE MODE")
    op.execute("UPDATE trades SET risk_reward = legacy_risk_reward WHERE k8_legacy_row")
    op.execute("""
        UPDATE trades t SET risk_reward = NULL
        WHERE NOT k8_legacy_row AND (
          status <> 'OPEN' OR exit_price IS NOT NULL OR
          EXISTS (SELECT 1 FROM trade_fills f WHERE f.trade_id = t.id AND f.fill_side = 'EXIT')
        )
    """)
    op.drop_column("trades", "k8_legacy_row")
    op.drop_column("trades", "legacy_risk_reward")
