"""Статическая проверка миграций: CHECK с IN (...) в downgrade — только в хранимой
форме Postgres (CLAUDE.md, «Конвенции»).

IN (...) Postgres хранит как (ARRAY[...])::text[], а копия репетиции после
dump → restore — поэлементно (ARRAY[('x'::character varying)::text, ...]):
через IN схема после downgrade ≠ baseline, репетиция падает на сверке схемы
(M5 08.10, M8 10.10.2026). Тест читает исходники alembic/versions через ast,
без базы: условие CHECK — строка в вызове или модульная константа.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
_IN = re.compile(r"\bIN\s*\(", re.IGNORECASE)
_NOT_LITERAL = "условие CHECK не строка — проверить вручную"

# Ревизия → почему IN в её downgrade не ломает сверку схемы. Каждая запись
# обязана реально срабатывать (test_allowed_entries_are_still_needed).
_ALLOWED = {
    # M1: после create_check_constraint тот же downgrade меняет тип колонки
    # action (String(16) → String(8)) — Postgres переписывает выражение CHECK в
    # хранимую поэлементную форму (комментарий _OLD_ACTIONS в M5).
    "5951467e6d9a": "alter_column той же колонки после CHECK",
}


def _constants(tree: ast.Module) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            out[node.targets[0].id] = node.value.value
    return out


def _text(node: ast.expr | None, consts: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    return None


def _attr(func: ast.expr) -> str:
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _check_conditions(call: ast.Call, consts: dict[str, str]) -> list[str | None]:
    """Условия CHECK, которые создаёт вызов (None — условие не строка)."""
    name = _attr(call.func)
    kw = {k.arg: k.value for k in call.keywords}
    if name == "create_check_constraint":
        return [_text(call.args[2] if len(call.args) > 2 else kw.get("condition"), consts)]
    if name == "CheckConstraint":
        return [_text(call.args[0] if call.args else kw.get("sqltext"), consts)]
    if name == "execute":
        sql = _text(call.args[0] if call.args else None, consts)
        if sql is not None and re.search(r"\bCHECK\b", sql, re.IGNORECASE):
            return [sql]
    return []


def violations(source: str) -> list[str]:
    """Строки «строка N: условие» для CHECK с IN в downgrade()."""
    tree = ast.parse(source)
    consts = _constants(tree)
    found: list[str] = []
    for fn in tree.body:
        if not (isinstance(fn, ast.FunctionDef) and fn.name == "downgrade"):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            for cond in _check_conditions(node, consts):
                if cond is None:
                    found.append(f"строка {node.lineno}: {_NOT_LITERAL}")
                elif _IN.search(cond):
                    found.append(f"строка {node.lineno}: {cond[:80]}")
    return found


def _revision(path: Path) -> str:
    return path.name.split("_", 1)[0]


def _scan() -> dict[str, list[str]]:
    return {
        _revision(p): v
        for p in sorted(_VERSIONS.glob("*.py"))
        if (v := violations(p.read_text(encoding="utf-8")))
    }


def test_no_check_with_in_in_downgrade() -> None:
    bad = {rev: v for rev, v in _scan().items() if rev not in _ALLOWED}
    assert not bad, (
        "CHECK с IN (...) в downgrade — записать в хранимой форме Postgres "
        "(ARRAY[('x'::character varying)::text, ...]), см. CLAUDE.md «Конвенции»: "
        f"{bad}"
    )


def test_allowed_entries_are_still_needed() -> None:
    found = _scan()
    stale = [rev for rev in _ALLOWED if rev not in found]
    assert not stale, f"исключения больше не нужны — убрать из _ALLOWED: {stale}"


def test_scanner_sees_versions() -> None:
    assert len(list(_VERSIONS.glob("*.py"))) >= 20


_BAD = '''
from alembic import op
import sqlalchemy as sa
_OLD = "action IN ('a', 'b')"
def upgrade():
    op.create_check_constraint("ck", "t", "action IN ('a', 'b', 'c')")
def downgrade():
    op.create_check_constraint("ck", "t", _OLD)
    op.create_check_constraint("ck2", "t", condition="kind in ('x')")
    op.create_table("t2", sa.Column("k"), sa.CheckConstraint("k IN ('y')", name="ck3"))
    op.execute("ALTER TABLE t ADD CONSTRAINT ck4 CHECK (s IN ('z'))")
'''

_GOOD = '''
from alembic import op
_OLD = "(action)::text = ANY (ARRAY[('a'::character varying)::text])"
def upgrade():
    op.create_check_constraint("ck", "t", "action IN ('a', 'b')")
def downgrade():
    op.create_check_constraint("ck", "t", _OLD)
    op.execute("DELETE FROM t WHERE action IN ('b')")
    op.create_check_constraint("ck2", "t", "price > 0")
'''


def test_scanner_catches_every_form() -> None:
    v = violations(_BAD)
    assert len(v) == 4, v


def test_scanner_ignores_upgrade_stored_form_and_plain_sql() -> None:
    assert violations(_GOOD) == []


def test_scanner_flags_non_literal_condition() -> None:
    src = (
        "from alembic import op\n"
        "def downgrade():\n"
        "    op.create_check_constraint('c', 't', f())\n"
    )
    assert violations(src) == [f"строка 3: {_NOT_LITERAL}"]
