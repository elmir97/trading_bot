"""K8 rehearsal entry point. Never accepts a production database destination."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.k8_rollback import BASE, HEAD, _alembic, capture, restore, rollback

COPY_DB = "trading_bot_rehearsal"


def guard(conn: Connection, database: str, address: str, version: str) -> None:
    """Check the destination on the operation's own connection before any write."""
    actual = conn.execute(sa.text(
        "SELECT current_database(), host(inet_server_addr()), "
        "current_setting('server_version_num')"
    )).one()
    if database != COPY_DB or tuple(actual) != (database, address, version):
        raise ValueError("K8 rehearsal destination/version mismatch")
    if conn.scalar(sa.text("SHOW search_path")) != '"$user", public':
        raise ValueError("K8 rehearsal requires the default search path")


def write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != "nt":
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def seal(path: Path) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    write_private(path.with_suffix(".sha256"), (digest + "\n").encode("ascii"))
    verify_seal(path)


def verify_seal(path: Path) -> None:
    if path.is_symlink() or path.with_suffix(".sha256").is_symlink():
        raise ValueError("K8 archive symlink")
    for item in (path, path.with_suffix(".sha256")):
        info = item.stat()
        if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and (
            stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid()
        )):
            raise ValueError("K8 archive permissions/type mismatch")
    if path.with_suffix(".sha256").read_text("ascii").strip() != hashlib.sha256(
        path.read_bytes()
    ).hexdigest():
        raise ValueError("K8 archive external checksum mismatch")


def data_snapshot(conn: Connection, *, invariant: bool = False) -> dict[str, str]:
    """Hash every public table without exporting its contents; include NULLs."""
    result = {}
    for table in sa.inspect(conn).get_table_names(schema="public"):
        if invariant and table == "alembic_version":
            continue
        quoted = conn.dialect.identifier_preparer.quote_identifier(table)
        expression = "to_jsonb(t)"
        if invariant and table == "trades":
            expression += " - 'risk_reward' - 'legacy_risk_reward' - 'k8_legacy_row'"
        digest = hashlib.sha256()
        for row in conn.execute(sa.text(
            f'SELECT ({expression})::text FROM public.{quoted} t '
            f'ORDER BY ({expression})::text COLLATE "C"'
        )):
            raw = row[0].encode("utf-8")
            digest.update(len(raw).to_bytes(8, "big"))
            digest.update(raw)
        result[table] = digest.hexdigest()
    return result


def schema_snapshot(conn: Connection) -> list[tuple]:
    return [tuple(row) for row in conn.execute(sa.text("""
        SELECT 'column', table_name::text, column_name::text,
          concat_ws('|', data_type, udt_name, numeric_precision, numeric_scale,
                    is_nullable, column_default)
        FROM information_schema.columns WHERE table_schema='public'
        UNION ALL
        SELECT 'constraint', c.relname::text, p.conname::text, pg_get_constraintdef(p.oid)
        FROM pg_constraint p JOIN pg_class c ON c.oid=p.conrelid
        WHERE p.connamespace='public'::regnamespace
        UNION ALL
        SELECT 'index', tablename::text, indexname::text, indexdef
        FROM pg_indexes WHERE schemaname='public'
        ORDER BY 1,2,3,4
    """))]


def validate_upgrade(before: list[dict[str, Any]], after: dict[str, Any]) -> None:
    rows = after["rows"]
    if len(before) != len(rows):
        raise ValueError("K8 upgrade changed row count")
    for old, new in zip(before, rows, strict=True):
        if (old["id"] != new["id"] or new["legacy"] != old["risk_reward"]
                or new["old"] is not True
                or new["planned"] != (old["risk_reward"] if new["eligible"] else None)):
            raise ValueError("K8 upgrade classification mismatch")


def synthetic(conn: Connection, directory: Path) -> None:
    """Exercise NULL/new rows/conflicts in a disposable transactional schema."""
    transaction = conn.begin_nested()
    try:
        schema = "k8_rehearsal_" + uuid4().hex
        conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        conn.execute(sa.text(f'SET LOCAL search_path TO "{schema}"'))
        conn.execute(sa.text("""
            CREATE TABLE trades (
              id integer PRIMARY KEY, risk_reward numeric(12,4), status text,
              source text DEFAULT 'MANUAL', fill_confirmed boolean DEFAULT true,
              exit_price numeric, updated_at timestamp, tag text);
            """))
        conn.execute(sa.text("""
            CREATE TABLE trade_fills (id integer PRIMARY KEY,
              trade_id integer, fill_side text, quantity numeric DEFAULT 1)
        """))
        conn.execute(sa.text("CREATE TABLE alembic_version (version_num varchar(32))"))
        conn.execute(sa.text("INSERT INTO alembic_version VALUES ('919ae8ad384e')"))
        conn.execute(sa.text("""
            INSERT INTO trades(id,risk_reward,status) VALUES
              (1,9,'CLOSED'),(2,NULL,'OPEN'),(3,2,'OPEN'),(8,8,'OPEN'),(9,9,'OPEN');
        """))
        conn.execute(sa.text("""
            INSERT INTO trade_fills(id,trade_id,fill_side) VALUES
              (1,3,'ENTRY'),(8,8,'ENTRY'),(9,8,'ENTRY'),(10,9,'ENTRY'),(11,9,'EXIT')
        """))
        _alembic(conn, HEAD, down=False)
        initial = capture(conn)
        if [(r["planned"], r["legacy"], r["old"]) for r in initial["rows"]] != [
            (None, "9.0000", True), (None, None, True), ("2.0000", "2.0000", True),
            (None, "8.0000", True), (None, "9.0000", True),
        ]:
            raise ValueError("K8 synthetic classification mismatch")
        conn.execute(sa.text("""
            INSERT INTO trades(id,risk_reward,status) VALUES
              (4,7,'CLOSED'),(5,3,'OPEN'),(6,NULL,'OPEN')
        """))
        conn.execute(sa.text("INSERT INTO trade_fills VALUES (2,5,'ENTRY',1)"))
        before = capture(conn)
        if any(r["old"] for r in before["rows"] if r["id"] in (4,5,6)):
            raise ValueError("K8 new-row boundary mismatch")
        rejected = False
        try:
            with conn.begin_nested():
                _alembic(conn, BASE, down=True)
        except sa.exc.DBAPIError:
            rejected = True
        if not rejected or capture(conn) != before:
            raise ValueError("K8 unverified downgrade was not atomic")
        archive = directory / "synthetic-1.json"
        rollback(conn, archive, seal)
        rr = dict(conn.execute(sa.text("SELECT id,risk_reward FROM trades")).all())
        if rr[1] != 9 or rr[2] is not None or rr[4] is not None or rr[5] != 3:
            raise ValueError("K8 synthetic downgrade mismatch")
        conn.execute(sa.text("INSERT INTO trades(id,risk_reward,status) VALUES (7,88,'CLOSED')"))
        _alembic(conn, HEAD, down=False)
        verify_seal(archive)
        restore(conn, archive)
        after = capture(conn)
        if [r for r in after["rows"] if r["id"] != 7] != before["rows"]:
            raise ValueError("K8 synthetic roundtrip mismatch")
        new = next(r for r in after["rows"] if r["id"] == 7)
        if (new["planned"], new["legacy"], new["old"]) != (None, "88.0000", True):
            raise ValueError("K8 old-code new row mismatch")
        second = directory / "synthetic-2.json"
        rollback(conn, second, seal)
        _alembic(conn, HEAD, down=False)
        for sql in (
            "UPDATE trades SET risk_reward=NULL WHERE id=5",
            "UPDATE trades SET risk_reward=4 WHERE id=5",
            "UPDATE trades SET tag='changed' WHERE id=1",
            "DELETE FROM trades WHERE id=1",
            "UPDATE trade_fills SET quantity=2 WHERE id=1",
            "DELETE FROM trade_fills WHERE id=1",
        ):
            with conn.begin_nested() as savepoint:
                conn.execute(sa.text(sql))
                changed = capture(conn)
                try:
                    restore(conn, second)
                except ValueError:
                    pass
                else:
                    raise ValueError("K8 restore accepted a conflict")
                if capture(conn) != changed:
                    raise ValueError("K8 conflict restore wrote partial data")
                savepoint.rollback()
        restore(conn, second)
        if capture(conn) != after:
            raise ValueError("K8 synthetic second roundtrip mismatch")
        print("K8 synthetic: OK; NULL/new rows; six atomic conflicts", flush=True)
    finally:
        transaction.rollback()


async def cycle(directory: Path, final_down: bool) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("K8 run directory must already exist")
    info = directory.stat()
    if os.name != "nt" and (stat.S_IMODE(info.st_mode) != 0o700
                             or info.st_uid != os.geteuid()):
        raise ValueError("K8 run directory must be owned by caller with mode 700")
    engine = create_async_engine(os.environ["DATABASE_URL"])
    expected = (os.environ["EXPECT_DB"], os.environ["EXPECT_ADDR"],
                os.environ["EXPECT_VERSION"])

    async def operation(fn):
        async with engine.begin() as conn:
            def checked(c):
                guard(c, *expected)
                c.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
                c.execute(sa.text("SET LOCAL statement_timeout = '120s'"))
                return fn(c)
            return await conn.run_sync(checked)

    try:
        def baseline(c):
            if c.scalar(sa.text("SELECT version_num FROM alembic_version")) != BASE:
                raise ValueError("K8 rehearsal requires exact base revision")
            rows = [dict(r) for r in c.execute(sa.text(
                "SELECT id, risk_reward::text AS risk_reward FROM trades ORDER BY id"
            )).mappings()]
            return data_snapshot(c), data_snapshot(c, invariant=True), schema_snapshot(c), rows
        original, invariant, base_schema, rows = await operation(baseline)
        def upgrade(c):
            _alembic(c, HEAD, down=False)
            upgraded = capture(c)
            validate_upgrade(rows, upgraded)
            if data_snapshot(c, invariant=True) != invariant:
                raise ValueError("K8 upgrade changed unrelated data")
            return upgraded
        upgraded = await operation(upgrade)
        upgraded_schema = await operation(schema_snapshot)
        first = directory / "rollback-1.json"
        await operation(lambda c: rollback(c, first, seal))
        if await operation(data_snapshot) != original:
            raise ValueError("K8 downgrade changed baseline data")
        if await operation(schema_snapshot) != base_schema:
            raise ValueError("K8 downgrade changed baseline schema")
        await operation(lambda c: _alembic(c, HEAD, down=False))
        if await operation(schema_snapshot) != upgraded_schema:
            raise ValueError("K8 repeated upgrade schema mismatch")
        verify_seal(first)
        await operation(lambda c: restore(c, first))
        if await operation(capture) != upgraded:
            raise ValueError("K8 restored state mismatch")
        if await operation(lambda c: data_snapshot(c, invariant=True)) != invariant:
            raise ValueError("K8 roundtrip changed unrelated data")
        if final_down:
            await operation(lambda c: rollback(c, directory / "rollback-2.json", seal))
            if await operation(data_snapshot) != original:
                raise ValueError("K8 second downgrade changed baseline data")
        await operation(lambda c: synthetic(c, directory))
        manifest = {"format": 1, "revision": HEAD, "database": COPY_DB,
                    "rows": len(rows), "image_id": os.environ["REHEARSAL_IMAGE_ID"],
                    "commit": os.environ["REHEARSAL_COMMIT"], "final_down": final_down,
                    "run_id": os.environ["REHEARSAL_RUN_ID"],
                    "server_version_num": expected[2], "copy_address": expected[1],
                    "archives": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in sorted(directory.glob("*.json"))}}
        write_private(directory / "k8-result.json", json.dumps(manifest).encode("utf-8"))
        print("K8 cycle: OK; archive checksums verified; baseline data preserved", flush=True)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--no-final-downgrade", action="store_true")
    args = parser.parse_args()
    asyncio.run(cycle(args.directory, not args.no_final_downgrade))


if __name__ == "__main__":
    main()
