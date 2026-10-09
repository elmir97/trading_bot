"""M7: execution_orders.cancel_source + разбор 45 строк прода (очередь A.3, 10.10.2026)

- execution_orders.cancel_source — VARCHAR(16) NULL: кто снял ордер у строки
  CANCELLED (EXCHANGE / BOT / USER, app.trading.enums.CancelSource). NULL у
  CANCELLED = «на бирже точно не стоит (подтверждено openOrders), кто снял —
  неизвестно». Без CHECK, как остальные Enum(native_enum=False) проекта.
- Данные: 45 строк прода по списку, утверждённому владельцем 09.10 (шаг 0,
  docs/handoff.md, «09.10 — ОЧЕРЕДЬ A»), группы A/B/D1/C/D2/E-USER/E-BOT и
  очистка ошибки строки 114. Только эти id; строки, появившиеся после 09.10,
  не трогаются.

Строка опознаётся по (id, client_order_id, статус до) — у 114 ещё по
error_code/error_message: голого id мало, в тестовой БД те же id заняты
чужими строками. У 15 строк прода client_order_id NULL (вложенные ордера,
строки-наблюдения отмен) — их отпечаток слабее, поэтому «прод или нет»
решают 30 строк с client_order_id («якоря»):
- не совпал ни один якорь — это не прод (тестовая или пустая база): данные
  пропускаются, WARNING «M7: данные пропущены»;
- совпали все якоря — обязаны совпасть все 45 строк, по каждой группе
  rowcount == числу id, иначе исключение;
- совпала часть якорей — исключение со списком расхождений.
Исключение откатывает upgrade целиком (одна транзакция, alembic/env.py).

Репетиция: --checksum --expect-columns execution_orders.cancel_source, без
--allow-data-change: upgrade меняет status/error_code/error_message, downgrade
обязан вернуть их точно — md5 после downgrade = baseline. Без --expect-null:
после upgrade колонка заполнена у 44 строк. В выводе upgrade — «M7: данные
применены — 45 строк»; «M7: данные пропущены» на копии прода — провал.

downgrade: строки, которые всё ещё в состоянии «после M7» (id, cid, статус,
cancel_source), возвращаются в исходное: A → CANCELED, B/D1/C/D2 → SUBMITTED,
у 114 — прежние error_code/error_message; E — только колонка. Строку, которую
после M7 изменил код, downgrade не трогает (WARNING с id) — откат не падает.
Затем колонка удаляется.

Revision ID: e7c41a9b3d52
Revises: dd2f284754a7
Create Date: 2026-10-10
"""

from __future__ import annotations

import logging
from typing import NamedTuple

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import context, op

revision = "e7c41a9b3d52"
down_revision = "dd2f284754a7"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")


class Group(NamedTuple):
    name: str
    before: str  # status до M7
    after: str   # status после M7
    source: str | None  # cancel_source после M7
    ids: tuple[int, ...]


class Spec(NamedTuple):
    groups: tuple[Group, ...]
    # client_order_id каждой строки; None — у строки его нет (NULL в базе).
    cids: dict[int, str | None]
    # Строка, у которой M7 очищает error_code/error_message, и их значения до M7.
    error_row: int
    error_before: tuple[str, str | None]

    def rows(self) -> list[tuple[Group, int]]:
        return [(g, i) for g in self.groups for i in g.ids]

    def anchors(self) -> set[int]:
        return {i for i, cid in self.cids.items() if cid is not None}


PROD = Spec(
    groups=(
        Group("A", "CANCELED", "CANCELLED", "EXCHANGE", (38, 39, 42, 45, 48, 50, 51)),
        Group("B", "SUBMITTED", "CANCELLED", "EXCHANGE", (
            84, 85, 90, 92, 96, 98, 106, 108, 114, 117, 122, 124, 128, 130,
        )),
        Group("D1", "SUBMITTED", "CANCELLED", "EXCHANGE", (63, 71, 78, 79)),
        Group("C", "SUBMITTED", "CANCELLED", "USER", (65, 67, 69)),
        Group("D2", "SUBMITTED", "FILLED", None, (75,)),
        Group("E-USER", "CANCELLED", "CANCELLED", "USER", (66, 68, 70, 72, 76, 77, 132)),
        Group("E-BOT", "CANCELLED", "CANCELLED", "BOT", (
            89, 91, 97, 107, 116, 123, 127, 129, 133,
        )),
    ),
    # client_order_id по SELECT на проде 10.10 (READ ONLY).
    cids={
        38: "tj209u1S", 39: "tj209u1T", 42: "tj215u1T", 45: "tj260u1T", 48: "tj304u1T",
        50: "tj305u1S", 51: "tj305u1T",
        63: "tm11u1T", 65: "tm13u1SB", 66: None, 67: "tm13u1S", 68: None, 69: "tm14u1SB",
        70: None, 71: "tm14u1S", 72: None, 75: "tm18u1S", 76: "tm19u1TB", 77: None,
        78: "tm19u1T", 79: "tm20u1S",
        84: None, 85: None, 89: None, 90: "to9u1s1", 91: None, 92: "to9u1t1",
        96: "to10u1s1", 97: None, 98: "to10u1t1", 106: "to12u1s2", 107: None,
        108: "to12u1t1", 114: "to14u1s1", 116: None, 117: "to14u1t1", 122: "to15u1s2",
        123: None, 124: "to15u1t1", 127: None, 128: "to16u1s1", 129: None,
        130: "to16u1t1", 132: "to17u1e", 133: "to19u1e",
    },
    error_row=114,
    error_before=("NOT_FOUND", "нет в openOrders, по clientOrderId не найден или снят"),
)

_ROW = sa.text(
    "SELECT 1 FROM execution_orders WHERE id = :id"
    " AND client_order_id IS NOT DISTINCT FROM :cid"
    " AND status = :status AND cancel_source IS NOT DISTINCT FROM :source"
)
_ERR = sa.text(
    "SELECT 1 FROM execution_orders WHERE id = :id"
    " AND error_code IS NOT DISTINCT FROM :code AND error_message IS NOT DISTINCT FROM :msg"
)


def _matches(conn: Connection, spec: Spec, *, after: bool) -> tuple[list[int], list[int]]:
    """id строк спецификации в состоянии «до» (after=False) или «после» M7 —
    (совпали, не совпали)."""
    code, msg = (None, None) if after else spec.error_before
    hit, miss = [], []
    for g, i in spec.rows():
        ok = conn.execute(_ROW, {
            "id": i, "cid": spec.cids[i],
            "status": g.after if after else g.before,
            "source": g.source if after else None,
        }).first() is not None
        if ok and i == spec.error_row:
            ok = conn.execute(_ERR, {"id": i, "code": code, "msg": msg}).first() is not None
        (hit if ok else miss).append(i)
    return hit, miss


def apply_data(conn: Connection, spec: Spec) -> int:
    """Разбор строк по spec. Число изменённых строк; 0 — не прод, пропуск."""
    hit, miss = _matches(conn, spec, after=False)
    if not spec.anchors() & set(hit):
        log.warning(
            "M7: данные пропущены — ни один из %d якорей (строк с client_order_id) не совпал"
            " (не прод)", len(spec.anchors()),
        )
        return 0
    if miss:
        raise RuntimeError(
            f"M7: совпала часть строк ({len(hit)} из {len(hit) + len(miss)}),"
            f" не совпали id {miss} — стоп, разобрать вручную"
        )
    for g in spec.groups:
        n = conn.execute(sa.text(
            "UPDATE execution_orders SET status = :after, cancel_source = :source"
            " WHERE id = ANY(:ids) AND status = :before AND cancel_source IS NULL"
        ), {"after": g.after, "source": g.source, "ids": list(g.ids), "before": g.before}).rowcount
        if n != len(g.ids):
            raise RuntimeError(f"M7: группа {g.name}: изменено {n} строк, ждали {len(g.ids)}")
        log.info("M7: группа %s — %d строк", g.name, n)
    code, msg = spec.error_before
    n = conn.execute(sa.text(
        "UPDATE execution_orders SET error_code = NULL, error_message = NULL"
        " WHERE id = :id AND error_code = :code AND error_message IS NOT DISTINCT FROM :msg"
    ), {"id": spec.error_row, "code": code, "msg": msg}).rowcount
    if n != 1:
        raise RuntimeError(f"M7: строка {spec.error_row}: ошибка не очищена (rowcount {n})")
    total = len(spec.rows())
    log.info("M7: данные применены — %d строк", total)
    return total


def revert_data(conn: Connection, spec: Spec) -> int:
    """Возврат строк, которые всё ещё в состоянии «после M7». Не падает:
    изменённые после M7 строки пропускаются с WARNING. Число возвращённых."""
    hit, miss = _matches(conn, spec, after=True)
    if not spec.anchors() & set(hit):
        log.warning("M7 downgrade: данные не возвращались — ни один якорь не в состоянии после M7")
        return 0
    if miss:
        log.warning("M7 downgrade: строки %s изменены после M7 — оставлены как есть", miss)
    keep = set(hit)
    for g in spec.groups:
        ids = [i for i in g.ids if i in keep]
        if ids and g.before != g.after:
            conn.execute(sa.text(
                "UPDATE execution_orders SET status = :before WHERE id = ANY(:ids)"
            ), {"before": g.before, "ids": ids})
    if spec.error_row in keep:
        code, msg = spec.error_before
        conn.execute(sa.text(
            "UPDATE execution_orders SET error_code = :code, error_message = :msg WHERE id = :id"
        ), {"id": spec.error_row, "code": code, "msg": msg})
    conn.execute(sa.text(
        "UPDATE execution_orders SET cancel_source = NULL WHERE id = ANY(:ids)"
    ), {"ids": sorted(keep)})
    log.info("M7 downgrade: возвращено %d строк", len(keep))
    return len(keep)


def _lit(value: str | None) -> str:
    return "NULL" if value is None else "'" + value.replace("'", "''") + "'"


def _offline_sql(spec: Spec, *, revert: bool) -> None:
    """--sql: те же UPDATE с отпечатком строки в WHERE; проверки якорей и
    rowcount — только онлайн."""
    op.execute("-- M7: проверки отпечатка и rowcount выполняются только онлайн")
    code, msg = spec.error_before
    for g, i in spec.rows():
        cid, src = _lit(spec.cids[i]), _lit(g.source)
        if revert:
            op.execute(
                f"UPDATE execution_orders SET status = '{g.before}', cancel_source = NULL"
                f" WHERE id = {i} AND client_order_id IS NOT DISTINCT FROM {cid}"
                f" AND status = '{g.after}' AND cancel_source IS NOT DISTINCT FROM {src}"
            )
        else:
            op.execute(
                f"UPDATE execution_orders SET status = '{g.after}', cancel_source = {src}"
                f" WHERE id = {i} AND client_order_id IS NOT DISTINCT FROM {cid}"
                f" AND status = '{g.before}' AND cancel_source IS NULL"
            )
    if revert:
        op.execute(
            f"UPDATE execution_orders SET error_code = {_lit(code)}, error_message = {_lit(msg)}"
            f" WHERE id = {spec.error_row} AND error_code IS NULL AND error_message IS NULL"
        )
    else:
        op.execute(
            "UPDATE execution_orders SET error_code = NULL, error_message = NULL"
            f" WHERE id = {spec.error_row} AND error_code = {_lit(code)}"
            f" AND error_message IS NOT DISTINCT FROM {_lit(msg)}"
        )


def upgrade() -> None:
    op.add_column(
        "execution_orders", sa.Column("cancel_source", sa.String(length=16), nullable=True)
    )
    if context.is_offline_mode():
        _offline_sql(PROD, revert=False)
    else:
        apply_data(op.get_bind(), PROD)


def downgrade() -> None:
    if context.is_offline_mode():
        _offline_sql(PROD, revert=True)
    else:
        revert_data(op.get_bind(), PROD)
    op.drop_column("execution_orders", "cancel_source")
