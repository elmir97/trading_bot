"""K8 real PostgreSQL rehearsal inside an isolated transactional test schema."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.k8_rollback import (
    HEAD,
    _alembic,
    capture,
    load_archive,
    restore,
    rollback,
    save_archive,
)


@pytest.mark.asyncio
async def test_roundtrip_with_old_null_and_new_rows(tmp_path: Path) -> None:
    raw = os.environ.get("DATABASE_URL", "")
    if not raw:
        pytest.skip("Need local trading_bot_test")
    url = sa.engine.make_url(raw)
    assert url.database == "trading_bot_test"
    assert url.host in ("localhost", "127.0.0.1", "::1")
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            try:
                schema = "k8_test_" + uuid4().hex
                await conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
                await conn.execute(sa.text(f'SET LOCAL search_path TO "{schema}"'))

                def exercise(c):
                    c.execute(sa.text("""
                        CREATE TABLE trades (
                          id integer PRIMARY KEY, risk_reward numeric(12,4), status text,
                          source text DEFAULT 'MANUAL', fill_confirmed boolean DEFAULT true,
                          exit_price numeric, updated_at timestamp, tag text)
                    """))
                    c.execute(sa.text("""
                        CREATE TABLE trade_fills (id integer PRIMARY KEY,
                          trade_id integer, fill_side text, quantity numeric DEFAULT 1)
                    """))
                    c.execute(sa.text("CREATE TABLE alembic_version (version_num varchar(32))"))
                    c.execute(sa.text("INSERT INTO alembic_version VALUES ('919ae8ad384e')"))
                    c.execute(sa.text("""
                        INSERT INTO trades(id,risk_reward,status) VALUES
                          (1,9,'CLOSED'),(2,NULL,'OPEN'),(3,2,'OPEN')
                    """))
                    c.execute(sa.text("INSERT INTO trade_fills VALUES (1,3,'ENTRY',1)"))
                    baseline_columns = sa.inspect(c).get_columns("trades")
                    _alembic(c, HEAD, down=False)
                    assert c.execute(sa.text(
                        "SELECT id,risk_reward,legacy_risk_reward,k8_legacy_row "
                        "FROM trades ORDER BY id"
                    )).all() == [(1, None, 9, True), (2, None, None, True), (3, 2, 2, True)]
                    c.execute(sa.text("""
                        INSERT INTO trades(id,risk_reward,status) VALUES
                          (4,7,'CLOSED'),(5,3,'OPEN'),(6,NULL,'OPEN')
                    """))
                    c.execute(sa.text("INSERT INTO trade_fills VALUES (2,5,'ENTRY',1)"))
                    assert c.scalar(sa.text(
                        "SELECT k8_legacy_row FROM trades WHERE id=6"
                    )) is False
                    before = capture(c)
                    # Plain downgrade cannot bypass archive verification.
                    with (
                        pytest.raises(sa.exc.DBAPIError, match="verified archive"), c.begin_nested()
                    ):
                        _alembic(c, "919ae8ad384e", down=True)
                    assert capture(c) == before
                    archive = tmp_path / "rollback.json"
                    rollback(c, archive)
                    assert str(sa.inspect(c).get_columns("trades")) == str(baseline_columns)
                    assert c.scalar(sa.text("SELECT risk_reward FROM trades WHERE id=1")) == 9
                    assert c.scalar(sa.text("SELECT risk_reward FROM trades WHERE id=2")) is None
                    assert c.scalar(sa.text("SELECT risk_reward FROM trades WHERE id=4")) is None
                    assert c.scalar(sa.text("SELECT risk_reward FROM trades WHERE id=5")) == 3
                    # A new old-code row between downgrade and the next upgrade.
                    c.execute(sa.text(
                        "INSERT INTO trades(id,risk_reward,status) VALUES (7,88,'CLOSED')"
                    ))
                    _alembic(c, HEAD, down=False)
                    assert restore(c, archive) == 6
                    after = capture(c)
                    assert after["rows"][:6] == before["rows"]
                    assert after["rows"][6]["planned"] is None
                    assert after["rows"][6]["legacy"] == "88.0000"
                    assert after["rows"][6]["old"] is True
                    # Rehearse another cycle; all original NULL boundaries survive.
                    second = tmp_path / "rollback2.json"
                    rollback(c, second)
                    _alembic(c, HEAD, down=False)
                    assert restore(c, second) == 7
                    assert capture(c) == after
                    third = tmp_path / "rollback3.json"
                    rollback(c, third)
                    _alembic(c, HEAD, down=False)
                    c.execute(sa.text("UPDATE trades SET risk_reward=NULL WHERE id=5"))
                    conflict = capture(c)
                    with pytest.raises(ValueError, match="planned RR changed"):
                        restore(c, third)
                    assert capture(c) == conflict
                    c.execute(sa.text("UPDATE trades SET risk_reward=3 WHERE id=5"))
                    c.execute(sa.text("UPDATE trade_fills SET quantity=2 WHERE id=1"))
                    unchanged = capture(c)
                    with pytest.raises(ValueError, match="changed"):
                        restore(c, third)
                    assert capture(c) == unchanged

                await conn.run_sync(exercise)
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


def test_archive_integrity_and_no_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "archive.json"
    payload = {"format": 1, "revision": HEAD, "database": "trading_bot_test", "rows": []}
    save_archive(path, payload)
    assert load_archive(path) == payload
    with pytest.raises(FileExistsError):
        save_archive(path, payload)
    envelope = json.loads(path.read_text())
    envelope["payload"]["database"] = "other"
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError, match="checksum"):
        load_archive(path)
