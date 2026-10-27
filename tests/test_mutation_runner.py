"""Прогонщик мутаций `scripts/mutation_runner.py` (CLAUDE.md, «прогонщик мутаций»): замена ровно
одна и с учётом CRLF, исходы CAUGHT/MISSED/INCONCLUSIVE, восстановление исходника, предел снимает
дерево процессов, журнал оборванного прогона, база перед мутациями."""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts import miniapp_scenarios as ms
from scripts import mutation_runner as mr

_ROOT = Path(__file__).resolve().parents[1]

# Проверка-заглушка: «тест» падает, если в target.txt есть MUT (так выглядит pytest -q).
_PROBE = (
    "import sys, pathlib\n"
    "t = pathlib.Path('target.txt').read_text(encoding='utf-8')\n"
    "if 'MUT' in t:\n"
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
    caught = t.classify(1, "F\nFAILED tests/test_x.py::test_a - boom\n1 failed in 1s\n")
    assert caught == mr.Outcome(mr.CAUGHT, "tests/test_x.py::test_a - boom")
    assert t.classify(0, "3 passed in 1s").verdict == mr.MISSED
    # Только errors (упала фикстура, нет базы) и exit 2 (сбор) — проверка не состоялась.
    assert t.classify(1, "2 errors in 1s").verdict == mr.INCONCLUSIVE
    assert t.classify(2, "1 error in 1s").verdict == mr.INCONCLUSIVE


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


def test_timeout_kills_whole_tree(tmp_path: Path) -> None:
    beat = tmp_path / "beat.txt"
    grandchild = (f"import time\nwhile True:\n    open({str(beat)!r}, 'a').write('.')\n"
                  "    time.sleep(0.1)\n")
    child = (f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', "
             f"{grandchild!r}])\ntime.sleep(60)\n")
    code, _ = mr.run_command([sys.executable, "-c", child], tmp_path, 3, mr.check_env())
    assert code is None
    assert beat.exists()                    # внук успел поработать
    time.sleep(0.5)
    size = beat.stat().st_size
    time.sleep(1.5)
    assert beat.stat().st_size == size      # и снят вместе с потомком


def test_timeout_outcome_inconclusive(tmp_path: Path) -> None:
    _target(tmp_path)
    m = mr.Mutation("m", "target.txt", Probe(code="import time; time.sleep(60)"), "alpha", "MUT")
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
    assert "итог: CAUGHT 1, MISSED 1, INCONCLUSIVE 0 из 2" in out
    assert (tmp_path / "target.txt").read_bytes() == b"alpha\nbeta\n"


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


def test_not_in_docker_image() -> None:
    lines = (_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "scripts/mutation_runner.py" in lines
