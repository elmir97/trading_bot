"""Own the local Windows Mini App stand, including its virtualenv launcher child."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = ROOT / "logs" / "stand"


class StandError(Exception):
    """An operation refused or could not prove that the stand has stopped."""


def process_birth(pid: int) -> int | None:
    """None means exited; access errors remain errors, never proof of absence."""
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:
            return None
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        wait = kernel.WaitForSingleObject(handle, 0)
        if wait == 0:
            return None
        if wait != 258:
            raise StandError("Cannot check process termination")
        fields = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(f) for f in fields)):
            raise ctypes.WinError(ctypes.get_last_error())
        return (fields[0].dwHighDateTime << 32) | fields[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def write_state(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    temporary.replace(path)


def read_state(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("root") != str(ROOT) or not isinstance(data.get("run"), str):
        raise StandError("State belongs to a different stand")
    if (type(data.get("port")) is not int or not 1024 <= data["port"] <= 65535
            or type(data.get("pid")) is not int or data["pid"] <= 0
            or type(data.get("birth")) is not int or data["birth"] <= 0):
        raise StandError("Invalid stand state; no process was stopped")
    return data


def port_free(port: int) -> bool:
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def healthy(port: int) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=0.5) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def owns_listener(port: int, pid: int) -> bool:
    result = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, check=True)
    endpoint = f"127.0.0.1:{port}".encode()
    rows = (line.split() for line in result.stdout.splitlines())
    return any(len(row) >= 5 and row[1] == endpoint and row[-1] == str(pid).encode()
               and row[2].endswith(b":0") for row in rows)


def identities(directory: Path, state: dict) -> list[dict]:
    records = [state]
    worker_path = directory / "worker.json"
    if worker_path.exists():
        worker = read_state(worker_path)
        if worker["run"] != state["run"]:
            raise StandError("Worker identity mismatch; no process was stopped")
        records.append(worker)
    for record in records:
        if not isinstance(record.get("pid"), int) or record["pid"] <= 0:
            raise StandError("Invalid PID")
        birth = process_birth(record["pid"])
        if birth is not None and birth != record.get("birth"):
            raise StandError("PID was reused; no process was stopped")
    return records


def command_matches(record: dict, state: dict, directory: Path) -> bool:
    # PID plus creation time protects against reuse; the nonce-bearing command
    # additionally prevents a corrupt state file from targeting an unrelated PID.
    command = ("$ErrorActionPreference='Stop'; "
               "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
               f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={record['pid']}'; "
               "if ($null -eq $p) {exit 3}; [Console]::Write($p.CommandLine)")
    args = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
    result = subprocess.run(args, capture_output=True, check=False)
    if result.returncode:
        raise StandError("Cannot verify process command line; no process was stopped")
    suffix = subprocess.list2cmdline([
        "-m", "scripts.stand", "_serve", "--state-dir", str(directory),
        "--port", str(state["port"]), "--run", state["run"],
    ])
    return result.stdout.decode("utf-8", errors="replace").endswith(suffix)


def stop(directory: Path, fallback_port: int = 8765) -> None:
    state_path = directory / "state.json"
    if not state_path.exists():
        if (directory / "worker.json").exists():
            raise StandError("Worker exists without controller state; refusing cleanup")
        if not port_free(fallback_port) or healthy(fallback_port):
            raise StandError(f"Port {fallback_port} is occupied by an unmanaged process")
        print(f"Stand already stopped; port {fallback_port} free")
        return
    state = read_state(state_path)
    records = identities(directory, state)
    for record in records:
        alive = process_birth(record["pid"]) is not None
        if alive and not command_matches(record, state, directory):
            raise StandError("Process command does not belong to this stand; refusing stop")
    for record in records:
        birth = process_birth(record["pid"])
        if birth is None:
            continue
        if birth != record["birth"]:
            raise StandError("PID identity changed before stop")
        result = subprocess.run(["taskkill", "/F", "/T", "/PID", str(record["pid"])],
                                capture_output=True, check=False)
        if result.returncode and process_birth(record["pid"]) is not None:
            raise StandError("taskkill failed; state retained")
    for _ in range(50):
        if (all(process_birth(r["pid"]) is None for r in records)
                and port_free(state["port"]) and not healthy(state["port"])):
            (directory / "worker.json").unlink(missing_ok=True)
            state_path.unlink()
            print(f"Stand tree stopped; port {state['port']} free; health unavailable")
            return
        time.sleep(0.1)
    raise StandError("Stop unconfirmed: process/port/health still present; state retained")


def start(directory: Path, port: int) -> None:
    if (directory / "state.json").exists() or (directory / "worker.json").exists():
        raise StandError("State already exists; use stop before another start")
    if not port_free(port) or healthy(port):
        raise StandError("Port occupied; no process was started or stopped")
    run = uuid4().hex
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    with (directory / "stand.log").open("ab") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "scripts.stand", "_serve", "--state-dir", str(directory),
             "--port", str(port), "--run", run], cwd=ROOT, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, creationflags=flags,
        )
    try:
        state = {"root": str(ROOT), "run": run, "pid": process.pid,
                 "birth": process_birth(process.pid), "port": port}
        if state["birth"] is None:
            raise StandError("Launcher exited before identity could be recorded")
        write_state(directory / "state.json", state)
    except BaseException:
        # Without the state file the bootstrap cannot start the HTTP server.
        # This Popen handle belongs to the process we just created, not a saved PID.
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)
        raise
    try:
        for _ in range(150):
            if process.poll() is not None:
                raise StandError("Stand exited during startup; see stand.log")
            worker_path = directory / "worker.json"
            if worker_path.exists():
                records = identities(directory, state)
                worker = records[-1]
                if owns_listener(port, worker["pid"]) and healthy(port):
                    print(f"Stand ready: http://127.0.0.1:{port}; launcher={process.pid}; "
                          f"worker={worker['pid']}; state={directory}")
                    return
            time.sleep(0.1)
        raise StandError("Startup timed out; see stand.log")
    except BaseException:
        stop(directory)
        raise


def serve(directory: Path, port: int, run: str) -> int:
    # The actual interpreter records itself before importing/starting the stand.
    for _ in range(100):
        path = directory / "state.json"
        if path.exists() and read_state(path)["run"] == run:
            break
        time.sleep(0.05)
    else:
        raise StandError("Missing launch authorization")
    write_state(directory / "worker.json", {"root": str(ROOT), "run": run,
                "pid": os.getpid(), "birth": process_birth(os.getpid()), "port": port})
    from scripts.miniapp_dev import main as dev_main

    return dev_main(["--port", str(port)])


@contextmanager
def locked(directory: Path):
    import msvcrt

    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "lock").open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise StandError("Another stand operation is running") from exc
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "status", "_serve"))
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--run")
    args = parser.parse_args()
    try:
        if os.name != "nt":
            raise StandError("This lifecycle helper supports Windows only")
        if not 1024 <= args.port <= 65535:
            raise StandError("Port must be between 1024 and 65535")
        directory = args.state_dir.resolve()
        if args.action == "_serve":
            return serve(directory, args.port, args.run)
        with locked(directory):
            if args.action == "start":
                start(directory, args.port)
            elif args.action == "stop":
                stop(directory, args.port)
            else:
                state = read_state(directory / "state.json")
                records = identities(directory, state)
                if (len(records) != 2 or not owns_listener(state["port"], records[-1]["pid"])
                        or not healthy(state["port"])):
                    raise StandError("Managed listener is not running")
                print(f"Stand running on {state['port']}")
        return 0
    except (StandError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Stand: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
