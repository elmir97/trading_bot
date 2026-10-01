"""scripts/rehearse_migration.sh — то, что можно проверить без Docker и прода.

Docker на этой машине не работает, сервер в тестах не участвует. Проверяется:
форма файла (LF, последняя строка, trap, обёртки dk/al), отказ на аргументах
до любого вызова docker, встроенные Python-фрагменты — meta_py на ревизиях
репозитория, guard_py на тестовой БД.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rehearse_migration.sh"
TEXT = SCRIPT.read_text(encoding="utf-8")
LINES = TEXT.splitlines()
# Строки кода: без комментариев целиком и без тел heredoc (Python и SQL).
_HEREDOC = re.compile(r"<<'(\w+)'")


def _code_lines() -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    end: str | None = None
    for no, line in enumerate(LINES, 1):
        if end is not None:
            if line == end:
                end = None
            continue
        if line.lstrip().startswith("#"):
            continue
        out.append((no, line))
        m = _HEREDOC.search(line)
        if m:
            end = m.group(1)
    return out


CODE = _code_lines()


def _function_body(name: str) -> list[str]:
    start = LINES.index(f"{name}() {{")
    end = LINES.index("}", start)
    return LINES[start + 1 : end]


def _heredoc(func: str) -> str:
    m = re.search(rf"^{func}\(\) {{\n    cat <<'(\w+)'\n(.*?)\n\1\n}}$", TEXT, re.S | re.M)
    assert m, func
    return m.group(2)


def _bash() -> str:
    """Git Bash на Windows (bash.exe из System32 — WSL, здесь не работает)."""
    if os.name == "nt":
        git = shutil.which("git")
        assert git, "нет git — не найти Git Bash"
        for parent in Path(git).resolve().parents:
            cand = parent / "bin" / "bash.exe"
            if cand.exists():
                return str(cand)
        pytest.fail("Git Bash не найден рядом с git")
    found = shutil.which("bash")
    assert found, "нет bash"
    return found


# --- форма файла ----------------------------------------------------------


def test_lf_only() -> None:
    assert b"\r" not in SCRIPT.read_bytes()


def test_last_line_is_main_then_exit() -> None:
    assert [ln for ln in LINES if ln.strip()][-1] == 'main "$@"; exit $?'


def test_main_starts_with_ignoring_sigpipe() -> None:
    body = [ln.strip() for ln in _function_body("main") if ln.strip()]
    assert body[0] == "trap '' PIPE"
    assert body[1:4] == ["exec </dev/null", "umask 077", "set -Eeuo pipefail"]


def test_traps_installed() -> None:
    body = {ln.strip() for ln in _function_body("main")}
    assert "trap on_exit EXIT" in body
    for sig in ("INT", "TERM", "HUP"):
        assert f"trap 'on_signal {sig}' {sig}" in body


def test_top_level_only_definitions() -> None:
    """Вне функций — только readonly-константы и вызов main: пока bash читает
    скрипт из stdin, ничего не выполняется."""
    depth = 0
    for _, line in CODE:
        if depth == 0 and line.strip():
            assert (
                line.startswith("readonly ")
                or re.match(r"^\w+\(\) \{", line)
                or line == 'main "$@"; exit $?'
            ), line
        if re.match(r"^\w+\(\) \{ .* \}$", line):
            continue
        if re.match(r"^\w+\(\) \{( #.*)?$", line):
            depth += 1
        elif line == "}":
            depth -= 1
    assert depth == 0


def test_docker_only_via_dk_and_restore_pipe() -> None:
    # docker в позиции команды; слово в тексте сообщений не в счёт.
    cmd = r"(^\s*|[|;&{(]\s*|\$\(\s*)docker\s"
    hits = [(no, ln) for no, ln in CODE if re.search(cmd, ln)]
    allowed = [
        (no, ln)
        for no, ln in hits
        if ln == 'dk() { docker "$@" </dev/null; }'
        or re.match(r'^\s*zcat "\$DUMP" \| docker exec -i ', ln)
    ]
    assert hits == allowed
    assert len(allowed) == 2


def test_alembic_only_via_al() -> None:
    sub = r"\balembic\s+(upgrade|downgrade|current|stamp|heads|history|revision|merge|check|show)\b"
    assert [ln for _, ln in CODE if re.search(sub, ln)] == []
    execs = [no for no, ln in CODE if "exec alembic" in ln]
    assert len(execs) == 1
    al_start = LINES.index("al() {") + 1
    al_end = LINES.index("}", al_start)
    assert al_start < execs[0] <= al_end
    assert any("dk run --rm --network \"$NET\"" in ln for ln in _function_body("al"))


@pytest.mark.parametrize(
    "pattern",
    [
        r"--env-file",
        r"\bcompose\s+(run|exec|up|build)\b",
        r"downgrade\s+-1",
        r"\brmi\s+(-f|--force)",
        r"\bdocker\s+run\b",
    ],
)
def test_forbidden_constructs(pattern: str) -> None:
    assert [ln for _, ln in CODE if re.search(pattern, ln)] == []


def test_copy_uses_compose_postgres_image() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    m = re.search(r"^  postgres:\n    image: (\S+)$", compose, re.M)
    assert m
    assert f"readonly PG_IMAGE={m.group(1)}" in LINES


def test_copy_container_limits() -> None:
    copy = "\n".join(_function_body("step_copy"))
    for part in (
        '--memory "$COPY_MEMORY"',
        "--tmpfs ",
        "--network \"$NET\"",
        "-c fsync=off -c full_page_writes=off",
    ):
        assert part in copy
    assert "readonly COPY_MEMORY=256m" in LINES
    assert "readonly TMPFS_MIN_MB=256" in LINES
    assert "network create --internal" in "\n".join(ln for _, ln in CODE)


def test_repo_lf_and_image_excludes() -> None:
    assert "*.sh text eol=lf" in (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "scripts/*.sh" in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()


def test_bash_syntax() -> None:
    res = subprocess.run(
        [_bash(), "-n", "scripts/rehearse_migration.sh"], cwd=ROOT, capture_output=True
    )
    assert res.returncode == 0, res.stderr.decode("utf-8", "replace")


# --- аргументы: отказ до любого docker ------------------------------------

IMG = ["--image", "trading_bot:rehearsal"]
FAKE = ["--source-container", "tb_fakeprod"]


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ([], "нужен --from"),
        (["--from", "abc"], "нужен --image"),
        (["--from"], "у --from нет значения"),
        (["--from", "a;b", *IMG], "не ревизия"),
        (["--from", "abc", "--image", "trading_bot-bot:latest"], "только trading_bot:rehearsal"),
        (["--from", "abc", "--image", "trading_bot-bot:rollback_20260929_120528"],
         "только trading_bot:rehearsal"),
        (["--from", "abc", *IMG, "--fault", "wrong-db"], "только с --source-container не прода"),
        (["--from", "abc", *IMG, "--fault", "wrong-db", "--source-container", "trading_bot_db"],
         "только с --source-container не прода"),
        (["--from", "abc", *IMG, *FAKE, "--fault", "wrong-db", "--rewind-to", "def"],
         "несовместим с --rewind-to"),
        (["--from", "abc", *IMG, *FAKE, "--fault", "other"], "известен только wrong-db"),
        (["--from", "abc", *IMG, "--rewind-to", "abc"], "перематывать нечего"),
        (["--from", "abc", *IMG, "--source-container", "tb_rehearsal_db_1"],
         "имена самого скрипта"),
        (["--from", "abc", *IMG, "--expect-columns", "signals"], "--expect-columns"),
        (["--from", "abc", *IMG, "--allow-count-change", "a.b"], "--allow-count-change"),
        (["--from", "abc", *IMG, "--allow-schema-diff", "a.b.c"], "--allow-schema-diff"),
        (["--from", "abc", *IMG, "--allow-data-change", "signals.atr"], "только с --checksum"),
        (["--from", "abc", *IMG, "--checksum", "--allow-data-change", "signals"],
         "--allow-data-change"),
        (["--from", "abc", *IMG, "--expect-null", "signals"], "--expect-null"),
        (["--from", "abc", *IMG, "--bogus"], "неизвестный аргумент"),
    ],
)
def test_refuses_bad_args(args: list[str], needle: str) -> None:
    res = subprocess.run(
        [_bash(), "scripts/rehearse_migration.sh", *args], cwd=ROOT, capture_output=True
    )
    err = res.stderr.decode("utf-8", "replace")
    assert res.returncode == 2, err
    assert needle in err
    assert "ОТКАЗ" in err


def test_help() -> None:
    res = subprocess.run(
        [_bash(), "scripts/rehearse_migration.sh", "--help"], cwd=ROOT, capture_output=True
    )
    assert res.returncode == 0
    assert "--allow-schema-diff" in res.stdout.decode("utf-8")


# --- meta_py: ревизии по файлам образа ------------------------------------

_SCRIPT_DIR = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
HEAD = _SCRIPT_DIR.get_current_head()
assert HEAD is not None
PREV = _SCRIPT_DIR.get_revision(HEAD).down_revision
assert isinstance(PREV, str)
PREV2 = _SCRIPT_DIR.get_revision(PREV).down_revision
assert isinstance(PREV2, str)


def _meta(base: str, frm: str, to: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONUTF8": "1"}
    return subprocess.run(
        [sys.executable, "-c", _heredoc("meta_py"), base, frm, to],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=env,
    )


def test_meta_one_step_to_head() -> None:
    res = _meta(PREV, PREV, "head")
    assert res.returncode == 0, res.stderr
    assert f"to={HEAD}" in res.stdout.splitlines()
    assert f"chain={HEAD}" in res.stdout.splitlines()


def test_meta_two_steps_in_order() -> None:
    res = _meta(PREV2, PREV2, HEAD)
    assert res.returncode == 0, res.stderr
    assert f"chain={PREV},{HEAD}" in res.stdout.splitlines()


def test_meta_rewind_from_between() -> None:
    res = _meta(PREV2, PREV, "head")
    assert res.returncode == 0, res.stderr


@pytest.mark.parametrize(
    ("base", "frm", "to", "needle"),
    [
        (HEAD, HEAD, "head", "нечего репетировать"),
        (PREV2, PREV2, PREV, "не head образа"),
        ("ffffffffffff", "ffffffffffff", "head", "нет цепочки"),
        (PREV, PREV2, "head", "не лежит между"),
    ],
)
def test_meta_refuses(base: str, frm: str, to: str, needle: str) -> None:
    """Код 2 — отказ по проверке: скрипт отвечает refuse (exit 2), не fail."""
    res = _meta(base, frm, to)
    assert res.returncode == 2, res.stderr
    assert needle in res.stderr


def test_meta_crash_is_not_refusal() -> None:
    """Сбой самого фрагмента (здесь — нет аргументов) — не 2: скрипт ответит fail."""
    res = subprocess.run(
        [sys.executable, "-c", _heredoc("meta_py")],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert res.returncode not in (0, 2)


# --- guard_py: сверка базы и адреса, против тестовой БД ---------------------


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужна тестовая БД (DATABASE_URL)")
def test_guard_matches_and_refuses() -> None:
    url = os.environ["DATABASE_URL"]
    db = url.rsplit("/", 1)[1].split("?")[0]

    def guard(expect_db: str, expect_addr: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "EXPECT_DB": expect_db, "EXPECT_ADDR": expect_addr}
        return subprocess.run(
            [sys.executable, "-c", _heredoc("guard_py")],
            capture_output=True, text=True, env=env,
        )

    wrong = guard(db, "203.0.113.1")
    assert wrong.returncode == 97, wrong.stderr
    m = re.match(r"guard: db=(\S+) addr=(\S+) MISMATCH$", wrong.stdout.strip())
    assert m and m.group(1) == db
    addr = m.group(2)

    assert guard(db, addr).returncode == 0
    assert guard("trading_bot_rehearsal", addr).returncode == 97


# --- SQL скрипта: каждый запрос src_sql/copy_sql — через psql, как в скрипте ---
#
# Поддельный docker SQL не выполняет: `ORDER BY 1 COLLATE "C"` дошёл до сервера
# (01.10, прогон 1). Здесь каждый запрос идёт тем же psql с теми же флагами и в
# READ ONLY (как src_sql на проде) против trading_bot_test.

_SQL_CALL = re.compile(r'\b(src_sql|copy_sql) "([^"]+)"')
_VAR_FROM_FUNC = re.compile(r"^\s*(\w+)=\$\((\w+_sql)\)$")
# Шаблон с подстановкой спецификации: X_SQL=${X_TEMPLATE//@SPEC@/…}
_VAR_FROM_TEMPLATE = re.compile(r"^\s*(\w+_SQL)=\$\{(\w+_TEMPLATE)//@SPEC@/.+\}$")
SPEC = "@SPEC@"


def _sql_calls() -> dict[str, str]:
    """Аргумент вызова → текст SQL (переменная раскрыта через свой heredoc;
    у шаблонных @SPEC@ остаётся — тест подставляет свою спецификацию)."""
    funcs = {m.group(1): m.group(2) for _, ln in CODE if (m := _VAR_FROM_FUNC.match(ln))}
    templates = {m.group(1): m.group(2) for _, ln in CODE if (m := _VAR_FROM_TEMPLATE.match(ln))}
    out: dict[str, str] = {}
    for _, ln in CODE:
        for m in _SQL_CALL.finditer(ln):
            arg = m.group(2)
            if not arg.startswith("$"):
                out[arg] = arg
                continue
            var = arg[1:]
            if var in templates:
                assert templates[var] in funcs, f"{templates[var]} не присвоен из функции-heredoc"
                sql = _heredoc(funcs[templates[var]])
                assert SPEC in sql
            else:
                assert var in funcs, f"{var} не присвоена из функции-heredoc"
                sql = _heredoc(funcs[var])
            out[arg] = sql
    return out


SQL_CALLS = _sql_calls()


def test_sql_call_sites_are_all_known() -> None:
    """Новый запрос в скрипте обязан попасть сюда — и в прогон ниже."""
    assert set(SQL_CALLS) == {
        "$COUNT_SQL",
        "$SCHEMA_SQL",
        "$CHECKSUM_SQL",
        "$NULL_SQL",
        "SELECT pg_database_size(current_database())",
        "SELECT version_num FROM alembic_version",
    }


def test_psql_only_in_sql_helpers_and_restore() -> None:
    # psql как команда (с флагами); слово в тексте сообщений не в счёт.
    psql = [ln for _, ln in CODE if re.search(r"\bpsql -X\b", ln)]
    assert len(psql) == 3
    assert sum('-c "$SQL"' in ln for ln in psql) == 2
    assert sum("zcat" in ln for ln in psql) == 1


def _psql() -> str:
    found = shutil.which("psql")
    if found:
        return found
    for cand in sorted(Path("C:/Program Files/PostgreSQL").glob("*/bin/psql.exe"), reverse=True):
        return str(cand)
    pytest.fail("psql не найден — SQL скрипта не проверить")


def _run_sql(sql: str) -> list[str]:
    url = urlsplit(os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1))
    env = {
        **os.environ,
        "PGPASSWORD": url.password or "",
        "PGOPTIONS": "-c default_transaction_read_only=on",
        "PGCLIENTENCODING": "UTF8",
    }
    res = subprocess.run(
        [
            _psql(), "-X", "-A", "-t", "-q", "-v", "ON_ERROR_STOP=1",
            "-h", url.hostname or "localhost", "-p", str(url.port or 5432),
            "-U", url.username or "", "-d", url.path.lstrip("/"), "-c", sql,
        ],
        capture_output=True, env=env,
    )
    err = res.stderr.decode("utf-8", "replace")
    assert res.returncode == 0, err
    assert err == "", err
    return res.stdout.decode("utf-8").splitlines()


def _c_sorted(rows: list[str]) -> bool:
    return rows == sorted(rows, key=lambda r: r.encode("utf-8"))


def _checksum_spec(exclude: frozenset[str] = frozenset()) -> str:
    """Спецификация md5 так же, как build_checksum_sql: все колонки снимка схемы
    по таблицам, «таблица:кол,кол;…», без exclude («таблица.кол»)."""
    cols: dict[str, list[str]] = {}
    for row in _run_sql(SQL_CALLS["$SCHEMA_SQL"]):
        kind, table, col = row.split("|")[:3]
        if kind == "col" and f"{table}.{col}" not in exclude:
            cols.setdefault(table, []).append(col)
    return ";".join(f"{t}:{','.join(c)}" for t, c in sorted(cols.items()))


def _sql_for(call: str) -> str:
    sql = SQL_CALLS[call]
    if call == "$CHECKSUM_SQL":
        return sql.replace(SPEC, _checksum_spec())
    if call == "$NULL_SQL":
        return sql.replace(SPEC, "alembic_version.version_num")
    return sql


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужна тестовая БД (DATABASE_URL)")
@pytest.mark.parametrize("call", sorted(SQL_CALLS))
def test_sql_runs_read_only_with_expected_shape(call: str) -> None:
    rows = _run_sql(_sql_for(call))
    assert rows
    if call == "$CHECKSUM_SQL":
        assert all(re.fullmatch(r"[A-Za-z0-9_]+\|[0-9a-f]{32}", r) for r in rows), rows
        assert _c_sorted(rows)
        tables = [r.split("|")[0] for r in _run_sql(SQL_CALLS["$COUNT_SQL"])]
        assert [r.split("|")[0] for r in rows] == tables
        return
    if call == "$NULL_SQL":
        assert rows == ["alembic_version.version_num|1"]
        return
    if call == "$COUNT_SQL":
        assert all(re.fullmatch(r"[A-Za-z0-9_]+\|\d+", r) for r in rows), rows
        assert "alembic_version|1" in rows
        assert _c_sorted(rows)
    elif call == "$SCHEMA_SQL":
        assert all(re.match(r"(col|idx|con)\|[^|]+\|[^|]+\|", r) for r in rows), rows[:5]
        assert "col|alembic_version|version_num|character varying(32)|NO|" in rows
        assert "con|alembic_version|alembic_version_pkc|p|PRIMARY KEY (version_num)" in rows
        assert any(r.startswith("idx|alembic_version|alembic_version_pkc|") for r in rows)
        assert _c_sorted(rows)
        assert len(rows) == len(set(rows))
    elif call.startswith("SELECT pg_database_size"):
        assert len(rows) == 1 and int(rows[0]) > 0
    else:
        assert len(rows) == 1
        assert _SCRIPT_DIR.get_revision(rows[0]) is not None


_needs_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="нужна тестовая БД (DATABASE_URL)"
)


@_needs_db
def test_checksum_equals_md5_of_row_text() -> None:
    """md5 = md5 строк ROW(…)::text через перевод строки; alembic_version — одна
    строка «(rev)»."""
    rev = _run_sql(SQL_CALLS["SELECT version_num FROM alembic_version"])[0]
    sql = SQL_CALLS["$CHECKSUM_SQL"].replace(SPEC, "alembic_version:version_num")
    assert _run_sql(sql) == [f"alembic_version|{hashlib.md5(f'({rev})'.encode()).hexdigest()}"]


@_needs_db
def test_checksum_is_deterministic() -> None:
    sql = SQL_CALLS["$CHECKSUM_SQL"].replace(SPEC, _checksum_spec())
    assert _run_sql(sql) == _run_sql(sql)


@_needs_db
def test_checksum_exclusion_changes_only_that_table() -> None:
    """--allow-data-change: исключённая колонка меняет md5 своей таблицы, и только её."""
    counts = dict(r.split("|") for r in _run_sql(SQL_CALLS["$COUNT_SQL"]))
    schema = _run_sql(SQL_CALLS["$SCHEMA_SQL"])
    cols: dict[str, list[str]] = {}
    for row in schema:
        kind, table, col = row.split("|")[:3]
        if kind == "col":
            cols.setdefault(table, []).append(col)
    table = next(
        (t for t in sorted(cols) if int(counts[t]) > 0 and len(cols[t]) >= 2), None
    )
    assert table, "в trading_bot_test нет непустой таблицы с ≥2 колонками"
    excluded = f"{table}.{cols[table][0]}"
    full = dict(
        r.split("|") for r in _run_sql(SQL_CALLS["$CHECKSUM_SQL"].replace(SPEC, _checksum_spec()))
    )
    part = dict(
        r.split("|")
        for r in _run_sql(
            SQL_CALLS["$CHECKSUM_SQL"].replace(SPEC, _checksum_spec(frozenset({excluded})))
        )
    )
    assert {t for t in full if full[t] != part[t]} == {table}
