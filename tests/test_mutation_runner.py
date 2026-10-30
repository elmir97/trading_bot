"""Прогонщик мутаций `scripts/mutation_runner.py` (CLAUDE.md, «прогонщик мутаций»): замена ровно
одна и с учётом CRLF, исходы CAUGHT/MISSED/BROKEN_MUTANT/INCONCLUSIVE (поймана — только падение на
проверке, 28.10), восстановление исходника, предел снимает дерево процессов, журнал оборванного
прогона, база перед мутациями."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts import miniapp_scenarios as ms
from scripts import mutation_runner as mr

_ROOT = Path(__file__).resolve().parents[1]

# Проверка-заглушка: «тест» падает на assert, если в target.txt есть MUT (так выглядит pytest -q:
# строка места трассировки с типом, итог -rf).
_PROBE = (
    "import sys, pathlib\n"
    "t = pathlib.Path('target.txt').read_text(encoding='utf-8')\n"
    "if 'MUT' in t:\n"
    "    print('E       assert 1 == 2'); print('tests/test_x.py:3: AssertionError')\n"
    "    print('FAILED tests/test_x.py::test_probe - mutated'); print('1 failed in 0.01s')\n"
    "    sys.exit(1)\n"
    "print('1 passed in 0.01s')\n"
)


@dataclass(frozen=True)
class Probe:
    code: str = _PROBE
    key: str = "probe"

    def argv(self, url: str) -> list[str]:
        return [sys.executable, "-c", self.code]

    def classify(self, code: int, out: str) -> mr.Outcome:
        return mr.Tests().classify(code, out)


def _target(root: Path, data: bytes = b"alpha\nbeta\n") -> Path:
    path = root / "target.txt"
    path.write_bytes(data)
    return path


# --- замена ------------------------------------------------------------------------------


def test_apply_lf_single_occurrence() -> None:
    out, why = mr.apply(b"a = 1\nb = 2\n", [("a = 1\nb", "a = 9\nb")])
    assert (out, why) == (b"a = 9\nb = 2\n", "")


def test_apply_respects_crlf_working_copy() -> None:
    out, why = mr.apply(b"a = 1\r\nb = 2\r\n", [("a = 1\nb = 2", "x\ny")])
    assert (out, why) == (b"x\r\ny\r\n", "")


@pytest.mark.parametrize(("text", "count"), [(b"x\n", 0), (b"a\na\n", 2)])
def test_apply_not_exactly_one_is_refused(text: bytes, count: int) -> None:
    assert mr.apply(text, [("a", "b")]) == (None, f"вхождений {count}")


def test_apply_list_each_exactly_one() -> None:
    assert mr.apply(b"a b\n", [("a", "c"), ("b", "d")]) == (b"c d\n", "")
    assert mr.apply(b"a b\n", [("a", "c"), ("z", "d")]) == (None, "замена 2: вхождений 0")


def test_mutation_pairs_length_mismatch() -> None:
    m = mr.Mutation("x", "f", mr.Tests("t"), ["a", "b"], ["c"])
    with pytest.raises(mr.RunnerError):
        m.pairs()


# --- исходы ------------------------------------------------------------------------------


def test_tests_classify() -> None:
    t = mr.Tests("tests/test_x.py")
    caught = t.classify(1, "E       assert 1 == 2\ntests/test_x.py:4: AssertionError\n"
                           "FAILED tests/test_x.py::test_a - boom\n1 failed in 1s\n")
    assert caught == mr.Outcome(mr.CAUGHT, "tests/test_x.py::test_a - boom")
    assert t.classify(0, "3 passed in 1s").verdict == mr.MISSED
    # Только errors (упала фикстура, нет базы) и exit 2 (сбор) — проверка не состоялась.
    assert t.classify(1, "2 errors in 1s").verdict == mr.INCONCLUSIVE
    assert t.classify(2, "1 error in 1s").verdict == mr.INCONCLUSIVE



def _failed(kind_line: str, place: str | None = None) -> str:
    """Вывод pytest -q -x -rf с одним падением: строка «E …», место трассировки, итог."""
    lines = [kind_line]
    if place is not None:
        lines.append(f"tests/test_x.py:7: {place}")
    return "\n".join([*lines, "FAILED tests/test_x.py::test_a - x", "1 failed in 0.1s"])


@pytest.mark.parametrize(("out", "verdict"), [
    (_failed("E       assert 1 == 2", "AssertionError"), mr.CAUGHT),
    (_failed("E       AssertionError: msg", "AssertionError"), mr.CAUGHT),
    (_failed("E       assert 1 == 2"), mr.CAUGHT),                       # без места — по «E assert»
    (_failed("E       Failed: nope"), mr.CAUGHT),                         # pytest.fail
    (_failed("E       Failed: DID NOT RAISE <class 'ValueError'>"), mr.CAUGHT),
    (_failed("E       NameError: name 'fmt_amount' is not defined", "NameError"),
     mr.BROKEN_MUTANT),                                                   # К4 F7/F8, 28.10
    (_failed("E       ImportError: cannot import name 'x'", "ImportError"), mr.BROKEN_MUTANT),
    (_failed("E   ModuleNotFoundError: No module named 'x'", "ModuleNotFoundError"),
     mr.BROKEN_MUTANT),
    (_failed("E       UnboundLocalError: x", "UnboundLocalError"), mr.BROKEN_MUTANT),
    (_failed("E       RuntimeError: x", "RuntimeError"), mr.INCONCLUSIVE),
    (_failed("E       KeyError: 'x'", "KeyError"), mr.INCONCLUSIVE),
])
def test_tests_classify_by_failure_type(out: str, verdict: str) -> None:
    """Поймана — только падение на проверке; поломка мутанта — BROKEN_MUTANT; иное исключение —
    не проверка (INCONCLUSIVE, разобрать руками)."""
    assert mr.Tests("tests/test_x.py").classify(1, out).verdict == verdict


def test_failure_type_crlf_output() -> None:
    """Вывод pytest на Windows — с CRLF: строка места трассировки читается и тогда, а в цепочке
    решает последнее исключение."""
    out = ("E           TimeoutError: x\r\n\r\ntest_m.py:5: TimeoutError\r\n\r\nDuring handling of "
           "the above exception, another exception occurred:\r\nE           Failed: nope\r\n\r\n"
           "test_m.py:7: Failed\r\nFAILED test_m.py::test_a - Failed: nope\r\n1 failed in 0.1s\r\n")
    assert mr.failure_type(out) == "Failed"
    assert mr.Tests("test_m.py").classify(1, out).verdict == mr.CAUGHT


def test_tests_classify_collection_and_fixture_errors() -> None:
    t = mr.Tests("tests/test_x.py")
    collect = ("_____ ERROR collecting tests/test_x.py _____\n"
               "E     File \"app/x.py\", line 3\nE       def f(:\nE   SyntaxError: invalid syntax\n"
               "!!!!! Interrupted: 1 error during collection !!!!!\n1 error in 0.4s\n")
    assert t.classify(2, collect).verdict == mr.BROKEN_MUTANT
    fixture_broken = ("E       NameError: name 'x' is not defined\ntests/conftest.py:9: NameError\n"
                      "1 error in 0.2s\n")
    assert t.classify(1, fixture_broken).verdict == mr.BROKEN_MUTANT
    fixture_env = ("E       OSError: connection refused\ntests/conftest.py:9: OSError\n"
                   "1 error in 0.2s\n")
    assert t.classify(1, fixture_env).verdict == mr.INCONCLUSIVE


@pytest.mark.parametrize(("body", "verdict"), [
    ("def test_a():\n    assert 1 == 2\n", mr.CAUGHT),
    ("def test_a():\n    assert [1] * 60 == [2] * 60, 'длинное сообщение ' * 10\n", mr.CAUGHT),
    ("import pytest\n\ndef test_a():\n    pytest.fail('нет')\n", mr.CAUGHT),
    ("import pytest\n\ndef test_a():\n    with pytest.raises(ValueError):\n        pass\n",
     mr.CAUGHT),
    ("def test_a():\n    undefined_name\n", mr.BROKEN_MUTANT),
    ("from os import no_such_name\n\ndef test_a():\n    pass\n", mr.BROKEN_MUTANT),
    ("def test_a(:\n", mr.BROKEN_MUTANT),
    # Цепочка: проверка поверх исходного исключения — решает последнее (pytest.fail), не первое.
    ("import pytest\n\ndef test_a():\n    try:\n        raise TimeoutError('x')\n"
     "    except TimeoutError:\n        pytest.fail('не завершился')\n", mr.CAUGHT),
    ("def test_a():\n    try:\n        assert 1 == 2\n    except AssertionError:\n"
     "        raise RuntimeError('поверх') from None\n", mr.INCONCLUSIVE),
    # Исключение при импорте — ошибка сбора: мутант сломал загрузку, не поведение.
    ("raise RuntimeError('при импорте')\n\ndef test_a():\n    pass\n", mr.BROKEN_MUTANT),
    ("def test_a():\n    raise RuntimeError('x')\n", mr.INCONCLUSIVE),
])
def test_tests_classify_real_pytest_output(tmp_path: Path, body: str, verdict: str) -> None:
    """Настоящий pytest (argv прогонщика, его окружение): разбор не зависит от ширины терминала
    — итог -rf в 80 колонок обрезает причину («- Asserti...»)."""
    (tmp_path / "test_m.py").write_text(body, encoding="utf-8")
    outcome = mr.run_check(mr.Tests("test_m.py"), tmp_path, "", 60)
    assert outcome.verdict == verdict, outcome

def test_tests_argv_captures_sys_and_stops_first() -> None:
    argv = mr.Tests("tests/a.py", args=["-k", "x"]).argv("")
    assert argv[:3] == [sys.executable, "-m", "pytest"]
    assert {"--capture=sys", "-x", "tests/a.py", "-k"} <= set(argv)


def test_scenario_classify() -> None:
    s = mr.Scenario("act_x")
    out = "✗ act_x — заголовок\n    ✗ «Да» ушло дважды\nсценариев 1, с замечаниями 1: act_x\n"
    assert s.classify(1, out) == mr.Outcome(mr.CAUGHT, "✗ «Да» ушло дважды")
    assert s.classify(0, "✓ act_x — заголовок").verdict == mr.MISSED
    assert s.classify(2, "").verdict == mr.INCONCLUSIVE
    # Замечание проверки со словами «не загрузился» (Plex) — не сеть.
    assert s.classify(1, "✗ act_x — t\n    ✗ Plex не загрузился: 'x'\n").verdict == mr.CAUGHT


@pytest.mark.parametrize("line", [
    f"    ✗ сценарий прерван: {ms.SDK_FAILED}",
    "    ✗ SDK Telegram не загрузился",
    "    ✗ сценарий упал: KeyError: 'x'",
    "    ✗ сценарий не уложился в 150 с",
    "    ✗ сценарий прерван: сброс стенда: HTTP 500",
])
def test_scenario_harness_failure_is_not_caught(line: str) -> None:
    assert mr.Scenario("act_x").classify(1, f"✗ act_x — t\n{line}\n").verdict == mr.INCONCLUSIVE


def test_harness_marks_match_real_texts() -> None:
    assert any(m in ms.SDK_FAILED for m in mr._HARNESS_MARKS)
    check_src = (_ROOT / "scripts" / "miniapp_browser_check.py").read_text(encoding="utf-8")
    assert '"SDK Telegram не загрузился"' in check_src
    scen_src = (_ROOT / "scripts" / "miniapp_scenarios.py").read_text(encoding="utf-8")
    for mark in ("сценарий упал:", "не уложился в", "сброс стенда:", "позиции стенда ("):
        assert mark in scen_src + check_src


# --- мутация и восстановление ------------------------------------------------------------


def test_run_mutation_caught_and_restored_crlf(tmp_path: Path) -> None:
    original = b"alpha\r\nbeta\r\n"
    path = _target(tmp_path, original)
    journal = tmp_path / "j.json"
    m = mr.Mutation("m", "target.txt", Probe(), "alpha\nbeta", "MUT\nbeta")
    outcome = mr.run_mutation(m, tmp_path, "", 30, journal)
    assert outcome == mr.Outcome(mr.CAUGHT, "tests/test_x.py::test_probe - mutated")
    assert path.read_bytes() == original
    assert not journal.exists()


def test_run_mutation_missed(tmp_path: Path) -> None:
    _target(tmp_path)
    m = mr.Mutation("m", "target.txt", Probe(), "beta", "gamma")
    assert mr.run_mutation(m, tmp_path, "", 30, tmp_path / "j.json").verdict == mr.MISSED


def test_run_mutation_ambiguous_is_inconclusive_untouched(tmp_path: Path) -> None:
    path = _target(tmp_path, b"a\na\n")
    m = mr.Mutation("m", "target.txt", Probe(), "a", "MUT")
    assert mr.run_mutation(m, tmp_path, "", 30, tmp_path / "j.json") == \
        mr.Outcome(mr.INCONCLUSIVE, "вхождений 2")
    assert path.read_bytes() == b"a\na\n"


def test_source_restored_when_check_raises(tmp_path: Path) -> None:
    path = _target(tmp_path)
    journal = tmp_path / "j.json"

    class Boom(Probe):
        def argv(self, url: str) -> list[str]:
            assert b"MUT" in path.read_bytes()      # мутация записана до проверки
            assert journal.exists()                 # и журнал — до неё
            raise KeyboardInterrupt

    m = mr.Mutation("m", "target.txt", Boom(), "alpha", "MUT")
    with pytest.raises(KeyboardInterrupt):
        mr.run_mutation(m, tmp_path, "", 30, journal)
    assert path.read_bytes() == b"alpha\nbeta\n"
    assert not journal.exists()


# Внук (и ребёнок) теста предела живёт не дольше этого, даже если прогон pytest убит жёстко и
# фикстура не отработала (27.10: три внука прерванного прогона сутки писали beat.txt — сняты 28.10).
GRANDCHILD_LIFETIME_S = 20


def _grandchild_code(beat: Path, pidfile: Path, lifetime: float) -> str:
    """Пишет свой PID и стучит в beat, пока не истечёт lifetime."""
    return (f"import os, time\nopen({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
            f"end = time.monotonic() + {lifetime}\n"
            f"while time.monotonic() < end:\n    open({str(beat)!r}, 'a').write('.')\n"
            "    time.sleep(0.1)\n")


def _alive(pid: int) -> bool:
    if os.name == "nt":
        # Байты: вывод tasklist — в OEM-кодировке консоли; CSV — точное «"PID"», не подстрока.
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                           capture_output=True, check=False)
        return f'"{pid}"'.encode() in r.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@pytest.fixture
def grandchild_pidfile(tmp_path: Path) -> Iterator[Path]:
    """PID внука — и гарантированное снятие в teardown, чем бы ни кончился тест."""
    pidfile = tmp_path / "grandchild.pid"
    yield pidfile
    if pidfile.exists() and pidfile.read_text().strip().isdigit():
        pid = int(pidfile.read_text())
        if _alive(pid):
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True,
                               check=False)
            else:
                os.kill(pid, 9)


def test_grandchild_stops_by_itself(tmp_path: Path, grandchild_pidfile: Path) -> None:
    """Самоограничение внука работает и без прогонщика: прерванный прогон его не оставит."""
    beat = tmp_path / "beat.txt"
    proc = subprocess.Popen([sys.executable, "-c", _grandchild_code(beat, grandchild_pidfile, 2)])
    deadline = time.monotonic() + 10
    while not (grandchild_pidfile.exists() and grandchild_pidfile.read_text().strip()):
        assert time.monotonic() < deadline, "внук не записал PID"
        time.sleep(0.05)
    assert _alive(int(grandchild_pidfile.read_text()))     # проверка «жив» видит живого
    try:
        code = proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pytest.fail("внук не завершился сам за 15 с (снимет фикстура)")
    assert code == 0
    assert not _alive(proc.pid)
    size = beat.stat().st_size
    time.sleep(0.5)
    assert beat.stat().st_size == size


def test_timeout_kills_whole_tree(tmp_path: Path, grandchild_pidfile: Path) -> None:
    beat = tmp_path / "beat.txt"
    grandchild = _grandchild_code(beat, grandchild_pidfile, GRANDCHILD_LIFETIME_S)
    child = (f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', "
             f"{grandchild!r}])\ntime.sleep({GRANDCHILD_LIFETIME_S})\n")
    code, _ = mr.run_command([sys.executable, "-c", child], tmp_path, 3, mr.check_env())
    assert code is None
    assert beat.exists()                    # внук успел поработать
    time.sleep(0.5)
    size = beat.stat().st_size
    time.sleep(1.5)
    assert beat.stat().st_size == size      # и снят вместе с потомком
    assert not _alive(int(grandchild_pidfile.read_text()))


def test_timeout_outcome_inconclusive(tmp_path: Path) -> None:
    _target(tmp_path)
    sleeper = Probe(code=f"import time; time.sleep({GRANDCHILD_LIFETIME_S})")
    m = mr.Mutation("m", "target.txt", sleeper, "alpha", "MUT")
    outcome = mr.run_mutation(m, tmp_path, "", 2, tmp_path / "j.json")
    assert outcome.verdict == mr.INCONCLUSIVE
    assert "предел 2 с" in outcome.detail
    assert (tmp_path / "target.txt").read_bytes() == b"alpha\nbeta\n"


# --- журнал оборванного прогона ----------------------------------------------------------


def test_restore_from_journal(tmp_path: Path) -> None:
    path = _target(tmp_path)
    journal = tmp_path / "j.json"
    mr.write_journal(journal, path, b"alpha\nbeta\n", b"MUT\nbeta\n")
    path.write_bytes(b"MUT\nbeta\n")                 # прогонщик убит после записи мутации
    assert "восстановлен" in mr.restore_from_journal(journal)
    assert path.read_bytes() == b"alpha\nbeta\n"
    assert not journal.exists()


def test_restore_refuses_foreign_edit(tmp_path: Path) -> None:
    path = _target(tmp_path)
    journal = tmp_path / "j.json"
    mr.write_journal(journal, path, b"alpha\nbeta\n", b"MUT\nbeta\n")
    path.write_bytes(b"edited by hand\n")
    with pytest.raises(mr.RunnerError):
        mr.restore_from_journal(journal)
    assert path.read_bytes() == b"edited by hand\n"
    assert journal.exists()


def test_restore_when_already_original(tmp_path: Path) -> None:
    path = _target(tmp_path)
    journal = tmp_path / "j.json"
    mr.write_journal(journal, path, path.read_bytes(), b"MUT\n")
    assert "уже исходный" in mr.restore_from_journal(journal)
    assert not journal.exists()


# --- main --------------------------------------------------------------------------------


def _spec(tmp_path: Path, probe_code: str = _PROBE) -> Path:
    spec = tmp_path / "spec.py"
    spec.write_text(
        "import sys\n"
        "from scripts.mutation_runner import Mutation, Tests\n"
        "class Probe:\n"
        "    key = 'probe'\n"
        f"    def argv(self, url): return [sys.executable, '-c', {probe_code!r}]\n"
        "    def classify(self, code, out): return Tests().classify(code, out)\n"
        "MUTATIONS = [\n"
        "    Mutation('alpha пойман', 'target.txt', Probe(), 'alpha', 'MUT'),\n"
        "    Mutation('beta пропущен', 'target.txt', Probe(), 'beta', 'gamma'),\n"
        "]\n", encoding="utf-8")
    return spec


def test_main_reports_counts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _target(tmp_path)
    code = mr.main([str(_spec(tmp_path))], root=tmp_path, journal=tmp_path / "j.json")
    out = capsys.readouterr().out
    assert code == 1
    assert "итог: CAUGHT 1, MISSED 1, BROKEN_MUTANT 0, INCONCLUSIVE 0 из 2" in out
    assert (tmp_path / "target.txt").read_bytes() == b"alpha\nbeta\n"



def test_main_broken_mutant_counted_and_not_green(tmp_path: Path,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    """Мутант сломал код (NameError) — BROKEN_MUTANT в итоге, прогон не зелёный (К4, 28.10)."""
    _target(tmp_path)
    probe = ("import sys, pathlib\n"
             "if 'MUT' in pathlib.Path('target.txt').read_text(encoding='utf-8'):\n"
             "    print(\"E       NameError: name 'x' is not defined\")\n"
             "    print('tests/test_x.py:3: NameError'); print('1 failed in 0.01s')\n"
             "    sys.exit(1)\n"
             "print('1 passed in 0.01s')\n")
    code = mr.main([str(_spec(tmp_path, probe)), "--only", "alpha"], root=tmp_path,
                   journal=tmp_path / "j.json")
    out = capsys.readouterr().out
    assert code == 1
    assert "BROKEN_MUTANT (NameError)" in out
    assert "итог: CAUGHT 0, MISSED 0, BROKEN_MUTANT 1, INCONCLUSIVE 0 из 1" in out

def test_main_only_all_caught_exit_0(tmp_path: Path) -> None:
    _target(tmp_path)
    assert mr.main([str(_spec(tmp_path)), "--only", "alpha"], root=tmp_path,
                   journal=tmp_path / "j.json") == 0


def test_main_red_baseline_stops_before_mutations(tmp_path: Path,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    _target(tmp_path)
    spec = _spec(tmp_path, probe_code="import sys; print('1 failed'); sys.exit(1)")
    assert mr.main([str(spec)], root=tmp_path, journal=tmp_path / "j.json") == 2
    captured = capsys.readouterr()
    assert "база не зелёная" in captured.err
    assert "итог:" not in captured.out


def test_main_refuses_with_pending_journal(tmp_path: Path,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    path = _target(tmp_path)
    journal = tmp_path / "j.json"
    mr.write_journal(journal, path, b"x", b"y")
    assert mr.main([str(_spec(tmp_path))], root=tmp_path, journal=journal) == 2
    assert "--restore" in capsys.readouterr().err


def test_main_scenario_needs_stand(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _target(tmp_path)
    spec = tmp_path / "spec.py"
    spec.write_text("from scripts.mutation_runner import Mutation, Scenario\n"
                    "MUTATIONS = [Mutation('m', 'target.txt', Scenario('act_x'), 'alpha', 'b')]\n",
                    encoding="utf-8")
    assert mr.main([str(spec), "--url", "http://127.0.0.1:9/"], root=tmp_path,
                   journal=tmp_path / "j.json") == 2
    assert "стенд не отвечает" in capsys.readouterr().err
    assert mr.main([str(spec), "--url", "http://example.com/"], root=tmp_path,
                   journal=tmp_path / "j.json") == 2


def test_cli_module_accepts_spec_classes(tmp_path: Path) -> None:
    """Под `python -m` спека импортирует scripts.mutation_runner — её Mutation должны приниматься
    (до правки: «в спеке нет непустого списка MUTATIONS из Mutation»)."""
    spec = _spec(tmp_path)
    # Своя временная папка: под прогонщиком журнал его мутации лежит в общей.
    env = {**mr.check_env(), "TMP": str(tmp_path), "TEMP": str(tmp_path), "TMPDIR": str(tmp_path)}
    r = subprocess.run([sys.executable, "-m", "scripts.mutation_runner", str(spec), "--only",
                        "нет такой"], cwd=_ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=60,
                       stdin=subprocess.DEVNULL, check=False)
    assert r.returncode == 2
    assert "--only не выбрал ни одной мутации" in r.stderr


def test_parse_args_spec_xor_restore() -> None:
    with pytest.raises(SystemExit):
        mr.parse_args([])
    with pytest.raises(SystemExit):
        mr.parse_args(["spec.py", "--restore"])


@pytest.mark.parametrize("encoding", ["cp1251", "ascii"])
@pytest.mark.parametrize("mode", ["caught", "mixed", "restore", "empty", "foreign",
                                 "missing", "args", "help"])
def test_cli_utf8_with_non_utf8_defaults(tmp_path: Path, encoding: str, mode: str) -> None:
    """Real CLI pipes must contain UTF-8, including restore and both error paths."""
    path = _target(tmp_path)
    original = path.read_bytes()
    journal = tmp_path / mr.JOURNAL.name
    probe = _PROBE.replace("Path('target.txt')", f"Path({str(path)!r})")
    spec = _spec(tmp_path, probe).read_text(encoding="utf-8")
    spec_path = tmp_path / "spec.py"
    spec_path.write_text(spec.replace("'target.txt', Probe()", f"{str(path)!r}, Probe()"),
                         encoding="utf-8")
    args = [str(spec_path)]
    stream, expected, code = "stdout", "итог: CAUGHT 1, MISSED 1", 1
    if mode == "caught":
        args += ["--only", "alpha"]
        expected, code = "итог: CAUGHT 1, MISSED 0", 0
    elif mode in ("restore", "empty", "foreign"):
        args = ["--restore"]
        expected, code = "журнала нет — восстанавливать нечего", 0
        if mode != "empty":
            mr.write_journal(journal, path, original, b"MUT\n")
            path.write_bytes(b"MUT\n" if mode == "restore" else b"foreign\n")
            expected = "восстановлен, журнал снят"
        if mode == "foreign":
            stream, expected, code = "stderr", "ни исходный, ни мутированный", 2
    elif mode == "missing":
        args = [str(tmp_path / "missing.py")]
        stream, expected, code = "stderr", "✗ Прогон не состоялся: спеки нет:", 2
    elif mode == "args":
        args = []
        stream, expected, code = "stderr", "нужна спека или --restore (одно из двух)", 2
    elif mode == "help":
        args = ["--help"]
        expected, code = "Прогон мутаций по спеке", 0
    env = {**os.environ, "PYTHONUTF8": "0", "PYTHONIOENCODING": encoding,
           "TMP": str(tmp_path), "TEMP": str(tmp_path), "TMPDIR": str(tmp_path)}
    bootstrap = (
        "import runpy, sys; "
        f"assert sys.stdout.encoding == sys.stderr.encoding == {encoding!r}; "
        "sys.argv = ['scripts.mutation_runner', *sys.argv[1:]]; "
        "runpy.run_module('scripts.mutation_runner', run_name='__main__')"
    )
    result = subprocess.run([sys.executable, "-c", bootstrap, *args], cwd=_ROOT,
                            env=env, capture_output=True, stdin=subprocess.DEVNULL,
                            timeout=60, check=False)
    assert result.returncode == code
    output = getattr(result, stream)
    assert expected.encode("utf-8") in output, output
    for data in (result.stdout, result.stderr):
        assert "\ufffd" not in data.decode("utf-8")
    if mode in ("caught", "mixed"):
        assert "alpha пойман".encode() in result.stdout
        assert "база:".encode() in result.stdout
    assert path.read_bytes() == (b"foreign\n" if mode == "foreign" else original)
    assert journal.exists() == (mode == "foreign")


def test_not_in_docker_image() -> None:
    lines = (_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "scripts/mutation_runner.py" in lines
