"""Real Windows shell loss and ownership regressions for a single gate phase."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import gate, stand

ROOT = Path(__file__).resolve().parents[1]
BASH = r"C:\Program Files\Git\bin\bash.exe"


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "Timed out waiting for owned process"
        time.sleep(0.05)


def shell_command(shell, argv):
    if shell == "powershell":
        quoted = " ".join("'" + str(arg).replace("'", "''") + "'" for arg in argv)
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "& " + quoted + "; exit $LASTEXITCODE"]
    import shlex

    return [BASH, "-c", shlex.join([str(arg) for arg in argv]) + "; exit $?"]


@pytest.mark.parametrize("shell", ["powershell", "bash"])
@pytest.mark.parametrize("code", [0, 1, 2])
def test_real_completion_preserves_exit_and_utf8_report(tmp_path, shell, code):
    report = tmp_path / "report"
    argv = [sys.executable, "-m", "scripts.gate", "--report-dir", report, "--",
            sys.executable, "-c", f"import sys; print('gate output'); sys.exit({code})"]
    result = subprocess.run(shell_command(shell, argv), cwd=ROOT, capture_output=True,
                            timeout=20, check=False)
    assert result.returncode == code, result.stderr
    data = json.loads((report / "result.json").read_text(encoding="utf-8"))
    assert data["exit"] == code and data["cleanup"] is True
    assert b"gate output" in (report / "stdout.log").read_bytes()


@pytest.mark.parametrize("shell", ["powershell", "bash"])
@pytest.mark.parametrize("loss", ["shell", "guard"])
def test_real_shell_loss_kills_owned_descendants_only(tmp_path, shell, loss):
    marker = tmp_path / "grandchild.json"
    child_marker = tmp_path / "child.json"
    later = tmp_path / "later.txt"
    report = tmp_path / "report"
    grandchild = (
        "import json,os,socket,time; from pathlib import Path; "
        "from scripts.stand import process_birth; "
        "s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); "
        f"Path({str(marker)!r}).write_text(json.dumps(dict(pid=os.getpid(), "
        "birth=process_birth(os.getpid()), port=s.getsockname()[1]))); time.sleep(20)"
    )
    grandchild_script = tmp_path / "grandchild.py"
    setup = f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
    grandchild_script.write_text(setup + grandchild, encoding="utf-8")
    child = (
        "import json,os,subprocess,sys,time; from pathlib import Path; "
        "from scripts.stand import process_birth; "
        f"Path({str(child_marker)!r}).write_text(json.dumps(dict(pid=os.getpid(), "
        "birth=process_birth(os.getpid())))); "
        f"subprocess.Popen([sys.executable,{str(grandchild_script)!r}]); time.sleep(20); "
        f"Path({str(later)!r}).write_text('must not run')"
    )
    child_script = tmp_path / "child.py"
    child_script.write_text(setup + child, encoding="utf-8")
    argv = [sys.executable, "-m", "scripts.gate", "--report-dir", report, "--",
            sys.executable, child_script]
    foreign = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    parent = subprocess.Popen(shell_command(shell, argv), cwd=ROOT,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    records = []
    try:
        wait_for(lambda: marker.exists() and marker.stat().st_size > 0)
        records = [json.loads(p.read_text()) for p in (marker, child_marker)]
        assert all(stand.process_birth(r["pid"]) == r["birth"] for r in records)
        port = records[0]["port"]
        assert not stand.port_free(port)
        started = time.monotonic()
        if loss == "shell":
            parent.terminate()  # Only the shell handle; never taskkill /T of the shell.
            parent.wait(timeout=5)
            wait_for(lambda: (report / "result.json").exists())
            data = json.loads((report / "result.json").read_text())
            assert data["exit"] == 130 and data["cleanup"] is True
            assert data["reason"] == "parent shell lost"
            assert parent.pid in data["shell_pids"]
        else:
            import _winapi

            data = json.loads((report / "state.json").read_text())
            birth = stand.process_birth(data["guard_pid"])
            handle = _winapi.OpenProcess(0x1000 | 1, False, data["guard_pid"])
            try:
                assert birth and stand.process_birth(data["guard_pid"]) == birth
                _winapi.TerminateProcess(handle, 137)
            finally:
                _winapi.CloseHandle(handle)
            wait_for(lambda: all(stand.process_birth(r["pid"]) is None for r in records))
            assert not (report / "result.json").exists()
        wait_for(lambda: stand.process_birth(data["guard_pid"]) is None)
        assert time.monotonic() - started < 10
        assert all(stand.process_birth(r["pid"]) is None for r in records)
        assert stand.port_free(port)
        assert foreign.poll() is None
        assert not later.exists()
    finally:
        if parent.poll() is None:
            parent.terminate()
        parent.wait(timeout=5)
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(foreign.pid)],
                       capture_output=True, check=False)
        foreign.wait(timeout=5)
        for record in records:
            if stand.process_birth(record["pid"]) == record["birth"]:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(record["pid"])],
                               capture_output=True, check=False)


class FakeWindows:
    def __init__(self, *, lost=False, cleanup_error=False, assign_error=False):
        self.lost, self.cleanup_error, self.assign_error = lost, cleanup_error, assign_error
        self.k = SimpleNamespace(CloseHandle=self.close)
        self.closed = []

    def shell(self):
        return 11, 100

    def job(self):
        return 22

    def assign_resume(self, job, process):
        if self.assign_error:
            raise gate.GuardError("assign failed")

    def alive(self, parent):
        return not self.lost

    def cleanup(self, job):
        if self.cleanup_error:
            raise gate.GuardError("unknown")

    def close(self, handle):
        self.closed.append(handle)


class FakeProcess:
    pid = 42

    def __init__(self, code):
        self.code, self.terminated = code, False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = 130

    def wait(self, timeout):
        assert timeout == gate.CLEANUP_TIMEOUT
        return self.code


@pytest.mark.parametrize("code", [0, 1, 2, 130])
def test_unknown_cleanup_overrides_main_result(monkeypatch, tmp_path, code):
    process = FakeProcess(code)
    monkeypatch.setattr(gate.subprocess, "Popen", lambda *a, **kw: process)
    api = FakeWindows(lost=code == 130, cleanup_error=True)
    assert gate.run(["unused"], tmp_path / "report", api) == 3
    data = json.loads((tmp_path / "report/result.json").read_text())
    assert data["cleanup"] is False
    assert api.closed == [22, 11]


def test_failed_assignment_terminates_only_new_suspended_process(monkeypatch, tmp_path):
    process = FakeProcess(None)
    def spawn(*args, **kwargs):
        assert kwargs["creationflags"] & 4
        return process
    monkeypatch.setattr(gate.subprocess, "Popen", spawn)
    assert gate.run(["unused"], tmp_path / "report", FakeWindows(assign_error=True)) == 2
    assert process.terminated


def test_parent_query_error_cannot_report_success(monkeypatch, tmp_path):
    monkeypatch.setattr(gate.subprocess, "Popen", lambda *a, **kw: FakeProcess(0))
    api = FakeWindows()
    def unknown(handle):
        raise PermissionError("unknown")
    monkeypatch.setattr(api, "alive", unknown)
    assert gate.run(["unused"], tmp_path / "report", api) == 2


def test_cleanup_failure_still_terminates_unassigned_launcher(monkeypatch, tmp_path):
    process = FakeProcess(None)
    monkeypatch.setattr(gate.subprocess, "Popen", lambda *a, **kw: process)
    api = FakeWindows(assign_error=True, cleanup_error=True)
    assert gate.run(["unused"], tmp_path / "report", api) == 3
    assert process.terminated


def test_cleanup_deadline_and_query_failure_are_not_success(monkeypatch):
    api = object.__new__(gate.Windows)
    api.k = type("Kernel", (), {"TerminateJobObject": lambda *args: 1})()
    monkeypatch.setattr(api, "active", lambda job: 1)
    times = iter([0, gate.CLEANUP_TIMEOUT + 1])
    monkeypatch.setattr(gate.time, "monotonic", lambda: next(times))
    with pytest.raises(gate.GuardError, match="not confirmed"):
        api.cleanup(22)
    def unknown(job):
        raise PermissionError("query failed")
    monkeypatch.setattr(api, "active", unknown)
    monkeypatch.setattr(gate.time, "monotonic", lambda: 0)
    with pytest.raises(PermissionError):
        api.cleanup(22)


def test_no_shell_refuses_before_spawning(monkeypatch, tmp_path):
    api = FakeWindows()
    def refuse():
        raise gate.GuardError("no shell")
    monkeypatch.setattr(api, "shell", refuse)
    monkeypatch.setattr(gate.subprocess, "Popen", lambda *a, **kw: pytest.fail("spawned"))
    assert gate.run(["unused"], tmp_path / "report", api) == 2


def test_existing_report_is_never_overwritten(tmp_path):
    (tmp_path / "result.json").write_text("kept")
    with pytest.raises(FileExistsError):
        gate.run(["unused"], tmp_path, FakeWindows())
    assert (tmp_path / "result.json").read_text() == "kept"


@pytest.mark.parametrize("state", ["valid", "reused", "missing"])
def test_shell_chain_checks_birth_against_each_child(monkeypatch, state):
    api = object.__new__(gate.Windows)
    closed = []
    api.k = SimpleNamespace(OpenProcess=lambda access, inherit, pid: pid,
                            CloseHandle=closed.append, Process32FirstW=None, Process32NextW=None)
    rows = [SimpleNamespace(pid=os.getpid(), parent=101, exe="python.exe"),
            SimpleNamespace(pid=101, parent=102, exe="python.exe"),
            SimpleNamespace(pid=102, parent=103, exe="bash.exe"),
            SimpleNamespace(pid=103, parent=104, exe="bash.exe"),
            SimpleNamespace(pid=104, parent=0, exe="other.exe")]
    if state == "missing":
        rows = rows[:-1]
    births = {os.getpid(): 300, 101: 200, 102: 250 if state == "reused" else 150, 103: 100}
    monkeypatch.setattr(api, "entries", lambda *args: iter(rows))
    monkeypatch.setattr(api, "handle_birth", births.__getitem__)
    monkeypatch.setattr(api, "alive", lambda handle: True)
    if state != "valid":
        with pytest.raises(gate.GuardError, match=r"reused|Incomplete"):
            api.shell()
        assert 102 in closed
    else:
        handles, pids = api.shell()
        assert handles == pids == [102, 103]
        assert 102 not in closed and 103 not in closed
