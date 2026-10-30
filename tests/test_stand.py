"""Lifecycle regressions without running the frontend stand alongside pytest."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import stand


def state(directory: Path):
    record = {"root": str(stand.ROOT), "run": "owned", "pid": 101,
              "birth": 1001, "port": 8765}
    stand.write_state(directory / "state.json", record)
    stand.write_state(directory / "worker.json", {**record, "pid": 102, "birth": 1002})
    return record


def stopping(monkeypatch, directory, births):
    killed = []
    monkeypatch.setattr(stand, "process_birth", lambda pid: births.get(pid))
    monkeypatch.setattr(stand, "command_matches", lambda *args: True)
    monkeypatch.setattr(stand, "port_free", lambda port: True)
    monkeypatch.setattr(stand, "healthy", lambda port: False)
    monkeypatch.setattr(stand.time, "sleep", lambda seconds: None)

    def kill(argv, **kwargs):
        pid = int(argv[-1])
        assert argv[:4] == ["taskkill", "/F", "/T", "/PID"]
        killed.append(pid)
        births.pop(pid, None)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(stand.subprocess, "run", kill)
    return killed


def test_orphan_worker_stopped_when_launcher_already_dead(monkeypatch, tmp_path):
    state(tmp_path)
    killed = stopping(monkeypatch, tmp_path, {102: 1002})
    stand.stop(tmp_path)
    assert killed == [102]
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "worker.json").exists()
    stand.stop(tmp_path)
    assert killed == [102]


def test_stop_both_launcher_and_worker(monkeypatch, tmp_path):
    state(tmp_path)
    killed = stopping(monkeypatch, tmp_path, {101: 1001, 102: 1002})
    stand.stop(tmp_path)
    assert killed == [101, 102]


@pytest.mark.parametrize("pid", [101, 102])
def test_reused_pid_refuses_before_any_kill(monkeypatch, tmp_path, pid):
    state(tmp_path)
    killed = stopping(monkeypatch, tmp_path, {101: 1001, 102: 1002, pid: 9999})
    with pytest.raises(stand.StandError, match="reused"):
        stand.stop(tmp_path)
    assert killed == []
    assert (tmp_path / "state.json").exists()


@pytest.mark.parametrize("reason", ["command", "access", "identity"])
def test_unknown_or_foreign_worker_refuses_all_deletions(monkeypatch, tmp_path, reason):
    state(tmp_path)
    killed = stopping(monkeypatch, tmp_path, {101: 1001, 102: 1002})
    if reason == "command":
        monkeypatch.setattr(stand, "command_matches", lambda *args: False)
    elif reason == "access":
        def denied(pid):
            raise PermissionError("cannot inspect process")
        monkeypatch.setattr(stand, "process_birth", denied)
    else:
        worker = json.loads((tmp_path / "worker.json").read_text())
        worker["run"] = "someone-else"
        stand.write_state(tmp_path / "worker.json", worker)
    with pytest.raises((stand.StandError, PermissionError)):
        stand.stop(tmp_path)
    assert killed == []


@pytest.mark.parametrize("failure", ["taskkill", "port", "health", "worker"])
def test_unconfirmed_stop_fails_and_keeps_state(monkeypatch, tmp_path, failure):
    state(tmp_path)
    stopping(monkeypatch, tmp_path, {101: 1001, 102: 1002})
    if failure in ("taskkill", "worker"):
        monkeypatch.setattr(stand.subprocess, "run", lambda argv, **kw:
                            subprocess.CompletedProcess(argv, int(failure == "taskkill")))
    elif failure == "port":
        monkeypatch.setattr(stand, "port_free", lambda port: False)
    else:
        monkeypatch.setattr(stand, "healthy", lambda port: True)
    with pytest.raises(stand.StandError):
        stand.stop(tmp_path)
    assert (tmp_path / "state.json").exists()


def test_missing_parent_state_never_adopts_worker(monkeypatch, tmp_path):
    state(tmp_path)
    (tmp_path / "state.json").unlink()
    killed = stopping(monkeypatch, tmp_path, {102: 1002})
    with pytest.raises(stand.StandError, match="without controller"):
        stand.stop(tmp_path)
    assert killed == []


def test_occupied_port_not_adopted(monkeypatch, tmp_path):
    monkeypatch.setattr(stand, "port_free", lambda port: False)
    monkeypatch.setattr(stand.subprocess, "Popen", lambda *a, **kw:
                        pytest.fail("Occupied port must refuse before spawning a process"))
    with pytest.raises(stand.StandError, match="Port occupied"):
        stand.start(tmp_path, 8765)
    with pytest.raises(stand.StandError, match="unmanaged"):
        stand.stop(tmp_path)
    assert not (tmp_path / "state.json").exists()


def test_repeated_stop_checks_requested_port(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(stand, "port_free", lambda port: seen.append(port) or True)
    monkeypatch.setattr(stand, "healthy", lambda port: False)
    stand.stop(tmp_path, 8766)
    assert seen == [8766]


def test_existing_state_refuses_duplicate_start(tmp_path):
    state(tmp_path)
    with pytest.raises(stand.StandError, match="already exists"):
        stand.start(tmp_path, 8765)


@pytest.mark.parametrize("field,value", [("port", "8765"), ("pid", -1), ("birth", None)])
def test_corrupt_state_refuses_before_kill(monkeypatch, tmp_path, field, value):
    record = state(tmp_path)
    record[field] = value
    stand.write_state(tmp_path / "state.json", record)
    killed = stopping(monkeypatch, tmp_path, {101: 1001, 102: 1002})
    with pytest.raises(stand.StandError, match="Invalid"):
        stand.stop(tmp_path)
    assert killed == []


def test_start_failure_calls_cleanup_of_recorded_launcher(monkeypatch, tmp_path):
    class Process:
        pid = 101

        def poll(self):
            return 2

    monkeypatch.setattr(stand, "port_free", lambda port: True)
    monkeypatch.setattr(stand, "healthy", lambda port: False)
    monkeypatch.setattr(stand, "process_birth", lambda pid: 1001)
    monkeypatch.setattr(stand.subprocess, "Popen", lambda *a, **kw: Process())
    cleaned = []
    monkeypatch.setattr(stand, "stop", lambda directory:
                        cleaned.append(stand.read_state(directory / "state.json")))
    with pytest.raises(stand.StandError, match="exited during startup"):
        stand.start(tmp_path, 8765)
    assert len(cleaned) == 1
    assert cleaned[0]["pid"] == 101


def test_cannot_save_pid_terminates_new_launcher(monkeypatch, tmp_path):
    events = []

    class Process:
        pid = 101

        def poll(self):
            return None

        def terminate(self):
            events.append("terminate")

        def wait(self, timeout):
            events.append("wait")

    monkeypatch.setattr(stand, "port_free", lambda port: True)
    monkeypatch.setattr(stand, "healthy", lambda port: False)
    monkeypatch.setattr(stand, "process_birth", lambda pid: 1001)
    monkeypatch.setattr(stand.subprocess, "Popen", lambda *a, **kw: Process())
    def cannot_write(*args):
        raise PermissionError("state directory cannot be written")
    monkeypatch.setattr(stand, "write_state", cannot_write)
    with pytest.raises(PermissionError):
        stand.start(tmp_path, 8765)
    assert events == ["terminate", "wait"]


def test_concurrent_operation_refused(tmp_path):
    with stand.locked(tmp_path):
        other = stand.locked(tmp_path)
        with pytest.raises(stand.StandError, match="Another stand"):
            other.__enter__()


def test_listener_requires_own_pid_and_listening_socket(monkeypatch):
    data = b"TCP 127.0.0.1:8765 0.0.0.0:0 LISTENING 102\r\n"
    monkeypatch.setattr(stand.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess([], 0, data))
    assert stand.owns_listener(8765, 102)
    assert not stand.owns_listener(8765, 999)
    assert not stand.owns_listener(8766, 102)


def test_command_nonce_is_required(monkeypatch, tmp_path):
    record = state(tmp_path)
    suffix = subprocess.list2cmdline([
        "-m", "scripts.stand", "_serve", "--state-dir", str(tmp_path),
        "--port", "8765", "--run", "owned",
    ])
    for command, expected in [("python.exe " + suffix, True), ("python.exe unrelated", False)]:
        monkeypatch.setattr(stand.subprocess, "run", lambda *a, text=command, **kw:
                            subprocess.CompletedProcess([], 0, text.encode()))
        assert stand.command_matches(record, record, tmp_path) is expected


def test_command_query_failure_is_not_permission_to_kill(monkeypatch, tmp_path):
    record = state(tmp_path)
    monkeypatch.setattr(stand.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess([], 1, b""))
    with pytest.raises(stand.StandError, match="Cannot verify"):
        stand.command_matches(record, record, tmp_path)


def test_foreign_real_process_survives_even_with_valid_birth(tmp_path):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                               creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        record = {"root": str(stand.ROOT), "run": "not-the-worker", "port": 8765,
                  "pid": process.pid, "birth": stand.process_birth(process.pid)}
        stand.write_state(tmp_path / "state.json", record)
        with pytest.raises(stand.StandError, match="does not belong"):
            stand.stop(tmp_path)
        assert process.poll() is None
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True)
        process.wait(timeout=5)


def test_shell_is_lf_and_syntax_valid():
    script = stand.ROOT / "scripts" / "stand.sh"
    assert b"\r" not in script.read_bytes()
    assert "scripts/stand.py" in (stand.ROOT / ".dockerignore").read_text().splitlines()
    # The project runs on Windows; do not use System32 bash (WSL).
    bash = r"C:\Program Files\Git\bin\bash.exe"
    result = subprocess.run([bash, "-n", str(script)], capture_output=True)
    assert result.returncode == 0, result.stderr


def test_shell_requires_explicit_interpreter(monkeypatch):
    monkeypatch.delenv("STAND_PYTHON", raising=False)
    result = subprocess.run([r"C:\Program Files\Git\bin\bash.exe", "scripts/stand.sh", "stop"],
                            cwd=stand.ROOT, capture_output=True)
    assert result.returncode == 2
    assert b"Set STAND_PYTHON" in result.stderr
