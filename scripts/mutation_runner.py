"""Прогонщик мутаций: замена в исходнике → проверка (pytest или сценарий в Chrome) → исход.

    python -m scripts.mutation_runner <спека.py> [--only <подстрока>…] [--timeout 240]
        [--url http://127.0.0.1:8765/]
    python -m scripts.mutation_runner --restore

Спека — модуль Python вне репозитория (scratchpad сессии) со списком `MUTATIONS`:

    from scripts.mutation_runner import Mutation, Scenario, Tests

    MUTATIONS = [
        Mutation("флаг риска всегда false", "miniapp/actions.js",
                 Scenario("act_risk_increase"),
                 "{ risk_confirmed: Boolean(card.risk_increase) });",
                 "{ risk_confirmed: false });"),
        Mutation("две замены в одном файле", "app/x.py", Tests("tests/test_x.py"),
                 ["old1", "old2"], ["new1", "new2"]),
    ]

Правила (CLAUDE.md, «прогонщик мутаций»):
- сначала база: каждая проверка спеки зелёная на чистом коде, иначе мутации не гоняются
  (exit 2);
- замена — с учётом CRLF рабочей копии (autocrlf), каждая — ровно одно вхождение, иначе
  INCONCLUSIVE;
- предел на мутацию (240 с): истёк — INCONCLUSIVE, дерево процессов снимается целиком
  (`taskkill /F /T` — `subprocess.run(timeout=…)` снимает только прямого потомка);
- исходник восстанавливается в `finally` с проверкой md5; до записи мутации — журнал во
  временной папке системы: прогонщик убит жёстко — следующий запуск откажется (exit 2),
  `--restore` вернёт файл;
- исход тестом: CAUGHT — pytest exit 1 с failed и падение на проверке (assert, AssertionError,
  pytest.fail / «DID NOT RAISE»); BROKEN_MUTANT — мутант сломал код, а не поведение (NameError,
  ImportError, ModuleNotFoundError, SyntaxError, IndentationError, ошибка сбора — владелец 28.10:
  такое не «поймана»); иное исключение (RuntimeError, KeyError…) и только errors без признака
  поломки — INCONCLUSIVE с типом (разобрать руками); MISSED — exit 0; исход сценарием: CAUGHT —
  exit 1 замечанием проверки, MISSED — exit 0; exit 2,
  «SDK … не загрузился», сбой обвязки (сценарий упал/не уложился, сброс стенда) — INCONCLUSIVE.

Сценарии — при запущенном стенде (`scripts/miniapp_dev.py`), статика читается с диска; стенд, Chrome
и pytest не параллельно. Эквивалентную мутацию (поведение не меняется) прогонщик не распознаёт — её
заменяют поведенческой и говорят об этом в отчёте.

Коды выхода: 0 — все CAUGHT; 1 — есть MISSED, BROKEN_MUTANT или INCONCLUSIVE; 2 — прогон не
состоялся (спека,
журнал оборванного прогона, база не зелёная, стенд не отвечает). В образ не попадает
(`.dockerignore`).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TIMEOUT = 240.0
DEFAULT_URL = "http://127.0.0.1:8765/"
JOURNAL = Path(tempfile.gettempdir()) / "trading_bot_mutation_pending.json"

CAUGHT = "CAUGHT"
MISSED = "MISSED"
INCONCLUSIVE = "INCONCLUSIVE"
BROKEN_MUTANT = "BROKEN_MUTANT"

# Падение на проверке — «поймана»: assert (pytest пишет AssertionError) и pytest.fail /
# pytest.raises «DID NOT RAISE» (Failed).
_CHECK_FAILURES = frozenset({"AssertionError", "Failed"})
# Мутант не загрузился или сослался на несуществующее имя — поломка кода, не поведение.
_BROKEN_ERRORS = frozenset({"NameError", "ImportError", "ModuleNotFoundError", "SyntaxError",
                            "IndentationError", "TabError", "UnboundLocalError"})
# Строка места в трассировке pytest: «tests/test_x.py:12: NameError».
_TB_PLACE = re.compile(r"^\S[^\n]*:\d+: ([A-Za-z_][\w.]*)$", re.MULTILINE)
_E_TYPE = re.compile(r"^E\s+([A-Za-z_][\w.]*)(?::|$)", re.MULTILINE)
_E_ASSERT = re.compile(r"^E\s+assert\b", re.MULTILINE)

# Сценарий прерван не проверкой, а окружением или обвязкой — «пойман» не считается.
_HARNESS_MARKS = ("SDK telegram.org не загрузился", "SDK Telegram не загрузился",
                  "сценарий упал:", "не уложился в", "сброс стенда:", "позиции стенда (")


class RunnerError(Exception):
    """Прогон не состоялся (exit 2)."""


@dataclass(frozen=True)
class Outcome:
    verdict: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.verdict} ({self.detail})" if self.detail else self.verdict


@dataclass(frozen=True, init=False)
class Tests:
    """Проверка pytest: пути (и при нужде аргументы вроде `-k`)."""

    paths: tuple[str, ...]
    args: tuple[str, ...] = ()

    def __init__(self, *paths: str, args: Sequence[str] = ()) -> None:
        object.__setattr__(self, "paths", tuple(paths))
        object.__setattr__(self, "args", tuple(args))

    @property
    def key(self) -> str:
        return "pytest " + " ".join(self.paths + self.args)

    def argv(self, url: str) -> list[str]:
        return [sys.executable, "-m", "pytest", *self.paths, *self.args, "-q", "-x",
                "--capture=sys", "-p", "no:cacheprovider", "-rf"]

    def classify(self, code: int, out: str) -> Outcome:
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        last = lines[-1] if lines else "?"
        if code == 0:
            return Outcome(MISSED, last)
        kind = failure_type(out)
        if code == 2 and "ERROR collecting" in out:
            return Outcome(BROKEN_MUTANT, f"ошибка сбора{f': {kind}' if kind else ''}")
        if kind in _BROKEN_ERRORS:
            return Outcome(BROKEN_MUTANT, kind)
        if code == 1 and " failed" in f" {last}":
            first = next((ln.removeprefix("FAILED ") for ln in lines if ln.startswith("FAILED ")),
                         last)
            if kind in _CHECK_FAILURES:
                return Outcome(CAUGHT, first[:140])
            return Outcome(INCONCLUSIVE, f"исключение {kind or '?'} — не проверка: {first[:100]}")
        return Outcome(INCONCLUSIVE, f"exit {code}: {last[:120]}")


def failure_type(out: str) -> str | None:
    """Тип падения в выводе pytest (-x — падение одно): по последней строке места трассировки —
    в цепочке («During handling of the above exception…») тест валит последнее исключение,
    исходное печатается первым (28.10: pytest.fail поверх TimeoutExpired); иначе по первой строке
    «E Тип: …» или «E assert …». Вывод на Windows — с CRLF: без замены «$» строки места не видит."""
    out = out.replace("\r\n", "\n")
    places = _TB_PLACE.findall(out)
    if places:
        return str(places[-1]).rsplit(".", 1)[-1]
    m = _E_TYPE.search(out)
    if m:
        return m.group(1).rsplit(".", 1)[-1]
    return "AssertionError" if _E_ASSERT.search(out) else None


@dataclass(frozen=True)
class Scenario:
    """Проверка сценарием `scripts.miniapp_browser_check --scenario <имя>` (стенд запущен)."""

    name: str

    @property
    def key(self) -> str:
        return f"сценарий {self.name}"

    def argv(self, url: str) -> list[str]:
        return [sys.executable, "-m", "scripts.miniapp_browser_check", "--scenario", self.name,
                "--url", url]

    def classify(self, code: int, out: str) -> Outcome:
        harness = next((ln.strip() for ln in out.splitlines()
                        if any(m in ln for m in _HARNESS_MARKS)), "")
        if code == 2:
            return Outcome(INCONCLUSIVE, "exit 2: окружение не готово")
        if harness:
            return Outcome(INCONCLUSIVE, f"обвязка/сеть: {harness[:110]}")
        if code == 0:
            return Outcome(MISSED)
        if code == 1:
            why = next((ln.strip() for ln in out.splitlines()
                        if ln.strip().startswith("✗ ")
                        and not ln.strip().startswith(f"✗ {self.name} ")), "")
            return Outcome(CAUGHT, why[:140])
        return Outcome(INCONCLUSIVE, f"exit {code}")


Check = Tests | Scenario


@dataclass(frozen=True)
class Mutation:
    """old/new — строка или списки одной длины (несколько замен в одном файле)."""

    name: str
    path: str
    check: Check
    old: str | Sequence[str]
    new: str | Sequence[str]

    def pairs(self) -> list[tuple[str, str]]:
        old = [self.old] if isinstance(self.old, str) else list(self.old)
        new = [self.new] if isinstance(self.new, str) else list(self.new)
        if len(old) != len(new) or not old:
            raise RunnerError(f"{self.name}: замен old {len(old)}, new {len(new)}")
        return list(zip(old, new, strict=True))


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def apply(original: bytes, pairs: list[tuple[str, str]]) -> tuple[bytes | None, str]:
    """Текст с заменами; None и причина — если какая-то замена не ровно одна."""
    crlf = b"\r\n" in original
    text = original.decode("utf-8")
    for i, (o, n) in enumerate(pairs, 1):
        if crlf:
            o, n = o.replace("\n", "\r\n"), n.replace("\n", "\r\n")
        count = text.count(o)
        if count != 1:
            where = f"замена {i}: " if len(pairs) > 1 else ""
            return None, f"{where}вхождений {count}"
        text = text.replace(o, n)
    return text.encode("utf-8"), ""


def _kill_tree(proc: subprocess.Popen[bytes]) -> bool:
    """Снять процесс со всеми потомками; True — снят."""
    if os.name == "nt":
        r = subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, check=False)
        killed = r.returncode == 0
    else:
        import signal

        try:
            os.killpg(proc.pid, signal.SIGKILL)
            killed = True
        except ProcessLookupError:
            killed = False
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        return False
    return killed or proc.poll() is not None


def run_command(argv: list[str], cwd: Path, timeout: float,
                env: dict[str, str]) -> tuple[int | None, str]:
    """Код выхода и вывод; код None — предел истёк, дерево снято."""
    kw: dict[str, object] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
        else {"start_new_session": True})
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, env=env, **kw)  # type: ignore[call-overload]
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if not _kill_tree(proc):
            print(f"  ⚠ дерево PID {proc.pid} не снято — проверь процессы руками", flush=True)
        try:
            out, _ = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            out = b""
        return None, out.decode("utf-8", "replace")
    return proc.returncode, out.decode("utf-8", "replace")


def check_env() -> dict[str, str]:
    return {**os.environ, "PYTHONUTF8": "1"}


def run_check(check: Check, root: Path, url: str, timeout: float) -> Outcome:
    code, out = run_command(check.argv(url), root, timeout, check_env())
    if code is None:
        return Outcome(INCONCLUSIVE, f"предел {timeout:g} с, дерево снято")
    return check.classify(code, out)


# --- журнал оборванного прогона ----------------------------------------------------------


def write_journal(journal: Path, path: Path, original: bytes, mutated: bytes) -> None:
    journal.write_text(json.dumps({
        "path": str(path), "original_md5": md5(original), "mutated_md5": md5(mutated),
        "original": base64.b64encode(original).decode("ascii")}), encoding="utf-8")


def restore_from_journal(journal: Path) -> str:
    """Вернуть исходник по журналу. Файл изменён не мутацией — отказ, журнал не трогать."""
    if not journal.exists():
        return "журнала нет — восстанавливать нечего"
    data = json.loads(journal.read_text(encoding="utf-8"))
    path = Path(data["path"])
    current = md5(path.read_bytes())
    if current == data["original_md5"]:
        journal.unlink()
        return f"{path}: уже исходный, журнал снят"
    if current != data["mutated_md5"]:
        raise RunnerError(f"{path}: ни исходный, ни мутированный (md5 {current}) — "
                          f"сверь руками, журнал {journal} оставлен")
    original = base64.b64decode(data["original"])
    path.write_bytes(original)
    if md5(path.read_bytes()) != data["original_md5"]:
        raise RunnerError(f"{path}: после восстановления md5 не совпал")
    journal.unlink()
    return f"{path}: восстановлен, журнал снят"


def run_mutation(m: Mutation, root: Path, url: str, timeout: float,
                 journal: Path = JOURNAL) -> Outcome:
    path = root / m.path
    original = path.read_bytes()
    mutated, why = apply(original, m.pairs())
    if mutated is None:
        return Outcome(INCONCLUSIVE, why)
    write_journal(journal, path, original, mutated)
    try:
        path.write_bytes(mutated)
        return run_check(m.check, root, url, timeout)
    finally:
        path.write_bytes(original)
        if md5(path.read_bytes()) != md5(original):
            raise RunnerError(f"{m.path} не восстановлен — журнал {journal} оставлен")
        journal.unlink(missing_ok=True)


# --- прогон ------------------------------------------------------------------------------


def load_spec(spec: Path) -> list[Mutation]:
    if not spec.is_file():
        raise RunnerError(f"спеки нет: {spec}")
    module_spec = importlib.util.spec_from_file_location("_mutation_spec", spec)
    if module_spec is None or module_spec.loader is None:
        raise RunnerError(f"спека не загружается: {spec}")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    mutations = getattr(module, "MUTATIONS", None)
    if not isinstance(mutations, list) or not mutations \
            or not all(isinstance(m, Mutation) for m in mutations):
        raise RunnerError("в спеке нет непустого списка MUTATIONS из Mutation")
    return mutations


def require_stand(url: str) -> None:
    if urlsplit(url).hostname != "127.0.0.1":
        raise RunnerError(f"стенд — только 127.0.0.1, а не {url}")
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/health", timeout=5) as r:
            ok = r.status == 200
    except OSError as exc:
        raise RunnerError(f"стенд не отвечает ({url}api/health): {exc}") from None
    if not ok:
        raise RunnerError(f"стенд: /api/health не 200 ({url})")


def baseline(mutations: list[Mutation], root: Path, url: str, timeout: float) -> None:
    """Каждая проверка спеки зелёная на чистом коде — иначе «пойманы» ничего не значат."""
    seen: dict[str, Check] = {}
    for m in mutations:
        seen.setdefault(m.check.key, m.check)
    for key, check in seen.items():
        code, out = run_command(check.argv(url), root, timeout, check_env())
        status = "предел истёк" if code is None else f"exit {code}"
        print(f"база: {key} — {status}", flush=True)
        if code != 0:
            tail = "\n".join(out.strip().splitlines()[-5:])
            raise RunnerError(f"база не зелёная: {key} ({status})\n{tail}")


def run_all(mutations: list[Mutation], root: Path, url: str, timeout: float,
            journal: Path = JOURNAL) -> dict[str, int]:
    width = max(len(m.name) for m in mutations)
    counts = {CAUGHT: 0, MISSED: 0, BROKEN_MUTANT: 0, INCONCLUSIVE: 0}
    for m in mutations:
        outcome = run_mutation(m, root, url, timeout, journal)
        counts[outcome.verdict] += 1
        print(f"{m.name:{width}}  [{m.check.key}]  {outcome}", flush=True)
    return counts


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m scripts.mutation_runner",
                                description="Прогон мутаций по спеке (CLAUDE.md).")
    p.add_argument("spec", nargs="?", type=Path, help="модуль со списком MUTATIONS")
    p.add_argument("--only", nargs="+", default=[],
                   help="только мутации, в имени которых есть одна из подстрок")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="предел на проверку, с")
    p.add_argument("--url", default=DEFAULT_URL, help="стенд для сценариев")
    p.add_argument("--restore", action="store_true",
                   help="вернуть исходник по журналу оборванного прогона")
    args = p.parse_args(argv)
    if args.restore == (args.spec is not None):
        p.error("нужна спека или --restore (одно из двух)")
    return args


def main(argv: list[str] | None = None, root: Path = ROOT, journal: Path = JOURNAL) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.restore:
            print(restore_from_journal(journal))
            return 0
        if journal.exists():
            raise RunnerError(f"прошлый прогон оборван — исходник может быть мутирован (журнал "
                              f"{journal}); сначала --restore")
        mutations = load_spec(args.spec)
        if args.only:
            mutations = [m for m in mutations if any(s in m.name for s in args.only)]
            if not mutations:
                raise RunnerError("--only не выбрал ни одной мутации")
        for m in mutations:
            m.pairs()
            if not (root / m.path).is_file():
                raise RunnerError(f"{m.name}: файла нет — {m.path}")
        if any(isinstance(m.check, Scenario) for m in mutations):
            require_stand(args.url)
        baseline(mutations, root, args.url, args.timeout)
        counts = run_all(mutations, root, args.url, args.timeout, journal)
    except RunnerError as exc:
        print(f"✗ Прогон не состоялся: {exc}", file=sys.stderr)
        return 2
    print(f"итог: CAUGHT {counts[CAUGHT]}, MISSED {counts[MISSED]}, "
          f"BROKEN_MUTANT {counts[BROKEN_MUTANT]}, INCONCLUSIVE {counts[INCONCLUSIVE]} "
          f"из {sum(counts.values())}")
    return 0 if counts[CAUGHT] == sum(counts.values()) else 1


if __name__ == "__main__":
    # Под -m этот файл — модуль __main__, а спека импортирует scripts.mutation_runner: два
    # разных класса Mutation, и isinstance в load_spec ложен. main — из импортируемого модуля.
    from scripts import mutation_runner

    sys.exit(mutation_runner.main())
