"""Destination guards and real PostgreSQL scenarios for isolated K8 rehearsal."""

import logging
import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.k8_rehearsal import (
    COPY_DB,
    data_snapshot,
    guard,
    schema_snapshot,
    seal,
    synthetic,
    validate_upgrade,
    verify_seal,
)


class Destination:
    def __init__(self, actual):
        self.actual = actual

    def execute(self, query):
        return self

    def one(self):
        return self.actual

    def scalar(self, query):
        return '"$user", public'


@pytest.mark.parametrize("actual,expected", [
    ((COPY_DB, "10.1.0.2", "160010"), (COPY_DB, "10.1.0.2", "160010")),
    (("production", "10.1.0.2", "160010"), (COPY_DB, "10.1.0.2", "160010")),
    ((COPY_DB, "10.1.0.3", "160010"), (COPY_DB, "10.1.0.2", "160010")),
    ((COPY_DB, "10.1.0.2", "160009"), (COPY_DB, "10.1.0.2", "160010")),
    (("production", "10.1.0.2", "160010"), ("production", "10.1.0.2", "160010")),
])
def test_destination(actual, expected):
    if actual == expected and actual[0] == COPY_DB:
        guard(Destination(actual), *expected)
    else:
        with pytest.raises(ValueError, match="destination/version"):
            guard(Destination(actual), *expected)


def test_seal_detects_corruption_and_no_overwrite(tmp_path: Path):
    archive = tmp_path / "archive.json"
    archive.write_bytes(b"original")
    seal(archive)
    verify_seal(archive)
    with pytest.raises(FileExistsError):
        seal(archive)
    archive.write_bytes(b"modified")
    with pytest.raises(ValueError, match="external checksum"):
        verify_seal(archive)


def test_upgrade_null_and_plan():
    before = [{"id": 1, "risk_reward": None}, {"id": 2, "risk_reward": "2.0000"}]
    rows = [{"id": 1, "legacy": None, "old": True, "planned": None, "eligible": False},
            {"id": 2, "legacy": "2.0000", "old": True,
             "planned": "2.0000", "eligible": True}]
    validate_upgrade(before, {"rows": rows})
    rows[0]["old"] = False
    with pytest.raises(ValueError, match="classification"):
        validate_upgrade(before, {"rows": rows})


@pytest.mark.asyncio
async def test_synthetic_real_postgres(tmp_path: Path):
    logger = logging.getLogger("app.k8_rehearsal_logging_probe")
    assert not logger.disabled
    url = sa.engine.make_url(os.environ["DATABASE_URL"])
    assert url.database == "trading_bot_test"
    assert url.host in ("localhost", "127.0.0.1", "::1")
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            snapshot = await conn.run_sync(data_snapshot)
            schema_before = await conn.run_sync(schema_snapshot)
            before = await conn.run_sync(lambda c: sa.inspect(c).get_schema_names())
            await conn.run_sync(lambda c: synthetic(c, tmp_path))
            assert not logger.disabled
            after = await conn.run_sync(lambda c: sa.inspect(c).get_schema_names())
            assert after == before
            assert await conn.run_sync(data_snapshot) == snapshot
            assert await conn.run_sync(schema_snapshot) == schema_before
            assert (tmp_path / "synthetic-1.json").exists()
            assert (tmp_path / "synthetic-2.json").exists()
    finally:
        await engine.dispose()
