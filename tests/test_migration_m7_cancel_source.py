"""M7 e7c41a9b3d52 (execution_orders.cancel_source + разбор строк прода) против
настоящей БД: apply_data/revert_data миграции на своей спецификации (отрицательные
id — в тестовой БД id прода заняты чужими строками), в транзакции с откатом.

Все совпали — применяется по группам и точно возвращается; ни одна — пропуск
(«не прод»); часть — исключение; downgrade не трогает строку, изменённую после
M7, и не падает."""

from __future__ import annotations

import importlib.util
import logging
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.core.config import Settings
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.services.user_service import UserService
from tests.conftest import cleanup_user

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic" / "versions" / "e7c41a9b3d52_execution_orders_cancel_source.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("m7_cancel_source", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m7 = _load()

SPEC = m7.Spec(
    groups=(
        m7.Group("A", "CANCELED", "CANCELLED", "EXCHANGE", (-1, -2)),
        m7.Group("B", "SUBMITTED", "CANCELLED", "EXCHANGE", (-3, -4)),
        m7.Group("C", "SUBMITTED", "CANCELLED", "USER", (-5,)),
        m7.Group("D2", "SUBMITTED", "FILLED", None, (-6,)),
        m7.Group("E-BOT", "CANCELLED", "CANCELLED", "BOT", (-7,)),
    ),
    # -7 — без client_order_id (как вложенные ордера прода): не якорь.
    cids={**{i: f"m7test{-i}" for i in range(-6, 0)}, -7: None},
    error_row=-4,
    error_before=("NOT_FOUND", "order not exist"),
)

_STATE = sa.text(
    "SELECT id, status, cancel_source, error_code, error_message FROM execution_orders"
    " WHERE id BETWEEN -7 AND -1 ORDER BY id DESC"
)


@pytest_asyncio.fixture
async def conn(unique_telegram_id: Callable[[], int]) -> AsyncIterator[Any]:
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    async with db.session() as session:
        user = await UserService(
            UserRepository(session), StrategyRepository(session),
            MistakeTypeRepository(session), settings,
        ).get_or_create(telegram_id=unique_telegram_id())
        await session.commit()
        user_id = user.id
    async with db.engine.connect() as c:
        tx = await c.begin()
        for g, i in SPEC.rows():
            err = SPEC.error_before if i == SPEC.error_row else (None, None)
            await c.execute(sa.text(
                "INSERT INTO execution_orders (id, user_id, client_order_id, symbol, side,"
                " position_side, order_type, role, status, error_code, error_message)"
                " VALUES (:id, :u, :cid, 'XRP-USDT', 'SELL', 'LONG', 'STOP_MARKET',"
                " 'STOP_LOSS', :st, :code, :msg)"
            ), {"id": i, "u": user_id, "cid": SPEC.cids[i], "st": g.before,
                "code": err[0], "msg": err[1]})
        yield c
        await tx.rollback()
    async with db.session() as session:
        fresh = await UserRepository(session).get_by_id(user_id)
        if fresh is not None:
            await cleanup_user(session, fresh)
            await session.commit()
    await db.dispose()


async def _state(c: Any) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in (await c.execute(_STATE)).all()]


async def _run(c: Any, fn: Callable[[Connection, Any], int]) -> int:
    return await c.run_sync(lambda sync: fn(sync, SPEC))


BEFORE = [
    (-1, "CANCELED", None, None, None),
    (-2, "CANCELED", None, None, None),
    (-3, "SUBMITTED", None, None, None),
    (-4, "SUBMITTED", None, "NOT_FOUND", "order not exist"),
    (-5, "SUBMITTED", None, None, None),
    (-6, "SUBMITTED", None, None, None),
    (-7, "CANCELLED", None, None, None),
]
AFTER = [
    (-1, "CANCELLED", "EXCHANGE", None, None),
    (-2, "CANCELLED", "EXCHANGE", None, None),
    (-3, "CANCELLED", "EXCHANGE", None, None),
    (-4, "CANCELLED", "EXCHANGE", None, None),
    (-5, "CANCELLED", "USER", None, None),
    (-6, "FILLED", None, None, None),
    (-7, "CANCELLED", "BOT", None, None),
]


async def test_all_match_applies_and_reverts_exactly(conn: Any) -> None:
    assert await _state(conn) == BEFORE
    assert await _run(conn, m7.apply_data) == 7
    assert await _state(conn) == AFTER
    assert await _run(conn, m7.revert_data) == 7
    assert await _state(conn) == BEFORE


async def test_no_anchor_match_skips_with_warning(
    conn: Any, caplog: pytest.LogCaptureFixture,
) -> None:
    """Якоря (строки с cid) не совпали — пропуск, даже если строка без cid
    совпала по id и статусу (в тестовой БД так и бывает)."""
    other = SPEC._replace(
        cids={i: (None if c is None else f"other{-i}") for i, c in SPEC.cids.items()}
    )
    with caplog.at_level(logging.WARNING, logger="alembic.runtime.migration"):
        assert await conn.run_sync(lambda sync: m7.apply_data(sync, other)) == 0
    assert "M7: данные пропущены" in caplog.text
    assert await _state(conn) == BEFORE


async def test_partial_match_raises_and_changes_nothing(conn: Any) -> None:
    await conn.execute(sa.text("UPDATE execution_orders SET status = 'FILLED' WHERE id = -5"))
    with pytest.raises(RuntimeError, match=r"совпала часть строк \(6 из 7\).*\[-5\]"):
        await _run(conn, m7.apply_data)
    assert [r[1] for r in await _state(conn)] == [
        "CANCELED", "CANCELED", "SUBMITTED", "SUBMITTED", "FILLED", "SUBMITTED", "CANCELLED",
    ]


async def test_anchors_match_but_row_without_cid_differs_raises(conn: Any) -> None:
    await conn.execute(sa.text("UPDATE execution_orders SET status = 'SUBMITTED' WHERE id = -7"))
    with pytest.raises(RuntimeError, match=r"совпала часть строк \(6 из 7\).*\[-7\]"):
        await _run(conn, m7.apply_data)


async def test_error_row_fingerprint_includes_error(conn: Any) -> None:
    await conn.execute(sa.text("UPDATE execution_orders SET error_message = 'x' WHERE id = -4"))
    with pytest.raises(RuntimeError, match=r"\[-4\]"):
        await _run(conn, m7.apply_data)


async def test_revert_leaves_row_changed_after_m7(
    conn: Any, caplog: pytest.LogCaptureFixture,
) -> None:
    await _run(conn, m7.apply_data)
    await conn.execute(sa.text("UPDATE execution_orders SET cancel_source = 'BOT' WHERE id = -3"))
    with caplog.at_level(logging.WARNING, logger="alembic.runtime.migration"):
        assert await _run(conn, m7.revert_data) == 6
    assert "[-3]" in caplog.text
    state = await _state(conn)
    assert state[2] == (-3, "CANCELLED", "BOT", None, None)
    assert [s for k, s in enumerate(state) if k != 2] == [
        b for k, b in enumerate(BEFORE) if k != 2
    ]


async def test_revert_without_m7_data_is_noop(conn: Any) -> None:
    assert await _run(conn, m7.revert_data) == 0
    assert await _state(conn) == BEFORE


def test_prod_spec_is_the_approved_list() -> None:
    """Утверждённый владельцем 09.10 список (docs/handoff.md): 45 строк, без
    повторов, cid у каждой, 114 — в группе B."""
    rows = m7.PROD.rows()
    ids = [i for _, i in rows]
    assert len(ids) == 45 and len(set(ids)) == 45
    assert set(m7.PROD.cids) == set(ids)
    # 30 якорей с client_order_id, 15 без (по SELECT на проде 10.10).
    assert len(m7.PROD.anchors()) == 30
    assert {i for i in ids if m7.PROD.cids[i] is None} == {
        66, 68, 70, 72, 77, 84, 85, 89, 91, 97, 107, 116, 123, 127, 129,
    }
    assert m7.PROD.error_row in m7.PROD.anchors()
    counts = {g.name: len(g.ids) for g in m7.PROD.groups}
    assert counts == {"A": 7, "B": 14, "D1": 4, "C": 3, "D2": 1, "E-USER": 7, "E-BOT": 9}
    assert m7.PROD.error_row == 114 and m7.PROD.error_before[0] == "NOT_FOUND"
    assert 114 in next(g for g in m7.PROD.groups if g.name == "B").ids
    by_status = {(g.before, g.after, g.source) for g in m7.PROD.groups}
    assert ("SUBMITTED", "FILLED", None) in by_status
