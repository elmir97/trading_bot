"""Verified K8 rollback archive for the separate local test database only."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import stat
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command

BASE = "919ae8ad384e"
HEAD = "c8a030202610"
ROOT = Path(__file__).resolve().parents[1]

# No notes, credentials or exchange responses are exported. Fingerprints cover
# all unchanged trade fields and executions, including IDs and NULL values.
FINGERPRINT = """
SELECT t.id, md5((to_jsonb(t) - 'risk_reward' - 'legacy_risk_reward'
                 - 'k8_legacy_row' - 'updated_at')::text ||
  COALESCE((SELECT jsonb_agg(to_jsonb(f) ORDER BY f.id)::text
            FROM trade_fills f WHERE f.trade_id = t.id), '[]')) AS fingerprint,
  t.risk_reward::text AS planned, t.legacy_risk_reward::text AS legacy,
  t.k8_legacy_row AS old,
  (t.status <> 'OPEN' OR t.exit_price IS NOT NULL OR EXISTS
   (SELECT 1 FROM trade_fills f WHERE f.trade_id=t.id AND f.fill_side='EXIT')) AS exited,
  (t.status = 'OPEN' AND t.exit_price IS NULL AND t.fill_confirmed
   AND t.source IN ('MANUAL', 'BOT', 'SIGNAL_EXECUTION')
   AND (SELECT count(*) FROM trade_fills f
        WHERE f.trade_id=t.id AND f.fill_side='ENTRY') = 1
   AND NOT EXISTS (SELECT 1 FROM trade_fills f
                   WHERE f.trade_id=t.id AND f.fill_side='EXIT')) AS eligible
FROM trades t ORDER BY t.id
"""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def capture(conn: Connection) -> dict[str, Any]:
    if conn.scalar(sa.text("SELECT version_num FROM alembic_version")) != HEAD:
        raise ValueError("K8 archive requires the exact K8 revision")
    return {
        "format": 1, "revision": HEAD,
        "database": conn.scalar(sa.text("SELECT current_database()")),
        "rows": [dict(row) for row in conn.execute(sa.text(FINGERPRINT)).mappings()],
    }


def save_archive(path: Path, payload: dict[str, Any]) -> None:
    envelope = {"payload": payload, "sha256": hashlib.sha256(_canonical(payload)).hexdigest()}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(_canonical(envelope))
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != "nt":
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def load_archive(path: Path) -> dict[str, Any]:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("K8 archive must be a regular file")
        if os.name != "nt" and (stat.S_IMODE(info.st_mode) != 0o600
                                 or info.st_uid != os.geteuid()):
            raise ValueError("K8 archive must be owned by the caller with mode 600")
        envelope = json.loads(stream.read())
    payload: dict[str, Any] = envelope["payload"]
    if envelope["sha256"] != hashlib.sha256(_canonical(payload)).hexdigest():
        raise ValueError("K8 archive checksum mismatch")
    if payload["format"] != 1 or payload["revision"] != HEAD:
        raise ValueError("Unsupported K8 archive")
    ids = [r["id"] for r in payload["rows"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate trade IDs in K8 archive")
    return payload


def _alembic(conn: Connection, target: str, *, down: bool) -> None:
    # Embedded Alembic must preserve the caller's logging configuration.
    # Loading alembic.ini triggers env.py/fileConfig and disables other loggers.
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.attributes["connection"] = conn
    (command.downgrade if down else command.upgrade)(config, target)


def rollback(
    conn: Connection, path: Path, verify: Callable[[Path], None] | None = None,
) -> None:
    conn.execute(sa.text("LOCK TABLE trades, trade_fills IN ACCESS EXCLUSIVE MODE"))
    payload = capture(conn)
    save_archive(path, payload)
    if load_archive(path) != capture(conn):
        raise ValueError("K8 archive read-back mismatch")
    if verify is not None:
        verify(path)
    conn.execute(sa.text("SELECT set_config('k8.rollback_verified', 'yes', true)"))
    _alembic(conn, BASE, down=True)
    conn.execute(sa.text("SELECT set_config('k8.rollback_verified', 'no', true)"))


def restore(conn: Connection, path: Path) -> int:
    """Restore only unchanged archived rows after an independent normal upgrade.

    New rows created while downgraded are absent from the archive and remain
    conservatively classified by the migration. Validate every row before writes.
    """
    payload = load_archive(path)
    conn.execute(sa.text("LOCK TABLE trades, trade_fills IN ACCESS EXCLUSIVE MODE"))
    current = capture(conn)
    if payload["database"] != current["database"]:
        raise ValueError("K8 archive database mismatch")
    rows = {r["id"]: r for r in current["rows"]}
    for saved in payload["rows"]:
        row = rows.get(saved["id"])
        expected = saved["legacy"] if saved["old"] else (
            None if saved["exited"] else saved["planned"]
        )
        if (row is None or row["fingerprint"] != saved["fingerprint"]
                or not row["old"] or row["legacy"] != expected):
            raise ValueError("K8 archive row changed, deleted or not freshly upgraded")
        # Normal upgrade may preserve only an untouched open plan, or clear it.
        if row["planned"] != (expected if row["eligible"] else None):
            raise ValueError("K8 planned RR changed after upgrade")
    for saved in payload["rows"]:
        conn.execute(sa.text("""
            UPDATE trades SET risk_reward = :planned, legacy_risk_reward = :legacy,
                              k8_legacy_row = :old WHERE id = :id
        """), {k: (Decimal(saved[k]) if k in ("planned", "legacy") and saved[k] is not None
                  else saved[k]) for k in ("id", "planned", "legacy", "old")})
    return len(payload["rows"])


async def run(action: str, path: Path) -> None:
    raw = os.environ.get("DATABASE_URL", "")
    url = make_url(raw)
    if url.database != "trading_bot_test" or url.host not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("K8 rollback tool is restricted to local trading_bot_test")
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            if action == "downgrade":
                await conn.run_sync(lambda c: rollback(c, path))
            else:
                await conn.run_sync(lambda c: restore(c, path))
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("downgrade", "restore"))
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    asyncio.run(run(args.action, args.archive))


if __name__ == "__main__":
    main()
