"""Run one foreground gate command, tied to its Windows parent shell."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes as w
from pathlib import Path

POLL = 0.1
CLEANUP_TIMEOUT = 5.0
SHELLS = {"powershell.exe", "pwsh.exe", "bash.exe", "cmd.exe"}


class GuardError(Exception):
    """The guard could not establish ownership or confirm cleanup."""


class ProcessEntry(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD),
                ("heap", ctypes.c_size_t), ("module", w.DWORD), ("threads", w.DWORD),
                ("parent", w.DWORD), ("priority", w.LONG), ("flags", w.DWORD),
                ("exe", w.WCHAR * 260)]


class ThreadEntry(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("tid", w.DWORD),
                ("pid", w.DWORD), ("base", w.LONG), ("delta", w.LONG), ("flags", w.DWORD)]


class BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", w.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                ("active_limit", w.DWORD), ("affinity", ctypes.c_size_t),
                ("priority", w.DWORD), ("scheduling", w.DWORD)]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]


class Accounting(ctypes.Structure):
    _fields_ = [("times", ctypes.c_int64 * 4), ("faults", w.DWORD),
                ("total", w.DWORD), ("active", w.DWORD), ("terminated", w.DWORD)]


class Windows:
    """Private unnamed Job Object: no PID-based tree deletion or breakaway."""

    def __init__(self):
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        declarations = {
            "CreateToolhelp32Snapshot": ([w.DWORD, w.DWORD], w.HANDLE),
            "Process32FirstW": ([w.HANDLE, ctypes.POINTER(ProcessEntry)], w.BOOL),
            "Process32NextW": ([w.HANDLE, ctypes.POINTER(ProcessEntry)], w.BOOL),
            "Thread32First": ([w.HANDLE, ctypes.POINTER(ThreadEntry)], w.BOOL),
            "Thread32Next": ([w.HANDLE, ctypes.POINTER(ThreadEntry)], w.BOOL),
            "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "OpenThread": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "ResumeThread": ([w.HANDLE], w.DWORD),
            "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
            "CloseHandle": ([w.HANDLE], w.BOOL),
            "CreateJobObjectW": ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
            "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            "QueryInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                           w.DWORD, ctypes.c_void_p], w.BOOL),
            "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
            "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
            "GetProcessTimes": ([w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4, w.BOOL),
        }
        for name, (args, result) in declarations.items():
            function = getattr(self.k, name)
            function.argtypes, function.restype = args, result

    def require(self, result):
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    def entries(self, kind, entry, first, following):
        handle = self.k.CreateToolhelp32Snapshot(kind, 0)
        if handle == w.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            row = entry()
            row.size = ctypes.sizeof(row)
            ok = first(handle, ctypes.byref(row))
            while ok:
                yield row
                ok = following(handle, ctypes.byref(row))
            if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.k.CloseHandle(handle)

    def shell(self):
        processes = {r.pid: (r.parent, r.exe.lower()) for r in self.entries(
            2, ProcessEntry, self.k.Process32FirstW, self.k.Process32NextW)}
        pid, seen, handles, pids = os.getpid(), set(), [], []
        child_birth = None
        complete = False
        try:
            while pid in processes and pid not in seen:
                seen.add(pid)
                parent, name = processes[pid]
                is_shell = pid != os.getpid() and name in SHELLS
                if handles and not is_shell:
                    complete = True
                    break
                handle = self.require(self.k.OpenProcess(0x1000 | 0x100000, False, pid))
                try:
                    birth = self.handle_birth(handle)
                    # Check every edge, including the venv and Git Bash launchers.
                    if child_birth is not None and birth > child_birth:
                        raise GuardError("Ancestor PID reused")
                    if not self.alive(handle):
                        raise GuardError("Ancestor already exited")
                    child_birth = birth
                    if is_shell:
                        handles.append(handle)
                        pids.append(pid)
                finally:
                    if handle not in handles:
                        self.k.CloseHandle(handle)
                pid = parent
            if not handles:
                raise GuardError("No supported parent shell; command not started")
            if not complete:
                raise GuardError("Incomplete parent shell ancestry; command not started")
            return handles, pids
        except BaseException:
            for handle in handles:
                self.k.CloseHandle(handle)
            raise

    def handle_birth(self, handle):
        fields = [w.FILETIME() for _ in range(4)]
        self.require(self.k.GetProcessTimes(handle, *(ctypes.byref(f) for f in fields)))
        return (fields[0].dwHighDateTime << 32) | fields[0].dwLowDateTime

    def alive(self, handle):
        if isinstance(handle, list):
            return all(self.alive(h) for h in handle)
        status = self.k.WaitForSingleObject(handle, 0)
        if status not in (0, 258):
            raise GuardError("Cannot check parent shell")
        return status == 258

    def job(self):
        job = self.require(self.k.CreateJobObjectW(None, None))
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            self.require(self.k.SetInformationJobObject(job, 9, ctypes.byref(limits),
                                                        ctypes.sizeof(limits)))
            return job
        except BaseException:
            self.k.CloseHandle(job)
            raise

    def assign_resume(self, job, process):
        # The launcher is suspended: it cannot spawn an interpreter before assignment.
        self.require(self.k.AssignProcessToJobObject(job, int(process._handle)))
        tids = [r.tid for r in self.entries(4, ThreadEntry, self.k.Thread32First,
                                           self.k.Thread32Next) if r.pid == process.pid]
        if len(tids) != 1:
            raise GuardError("Cannot identify suspended primary thread")
        thread = self.require(self.k.OpenThread(2, False, tids[0]))
        try:
            if self.k.ResumeThread(thread) != 1:
                raise GuardError("Cannot resume owned command")
        finally:
            self.k.CloseHandle(thread)

    def active(self, job):
        info = Accounting()
        self.require(self.k.QueryInformationJobObject(job, 1, ctypes.byref(info),
                                                      ctypes.sizeof(info), None))
        return info.active

    def cleanup(self, job):
        self.require(self.k.TerminateJobObject(job, 130))
        deadline = time.monotonic() + CLEANUP_TIMEOUT
        while self.active(job):
            if time.monotonic() >= deadline:
                raise GuardError("Job cleanup not confirmed")
            time.sleep(POLL)


def run(command: list[str], directory: Path, api=None) -> int:
    """Preserve the command exit code; 130 parent lost, 3 cleanup unconfirmed, 2 refusal."""
    api = api or Windows()
    directory.mkdir(parents=True, exist_ok=False)
    result = {"exit": 2, "cleanup": False, "reason": "not started", "guard_pid": os.getpid()}
    parent = job = process = None
    try:
        parent, result["shell_pids"] = api.shell()
        job = api.job()
        with (directory / "stdout.log").open("wb") as output:
            process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, creationflags=0x4 | 0x200)
            result["pid"] = process.pid
            (directory / "state.json").write_text(json.dumps(result), encoding="utf-8")
            api.assign_resume(job, process)
            while True:
                if not api.alive(parent):
                    result.update(exit=130, reason="parent shell lost")
                    break
                code = process.poll()
                if code is not None:
                    result.update(exit=code, reason="command completed")
                    break
                time.sleep(POLL)
    except (OSError, GuardError) as exc:
        result.update(exit=2, reason=type(exc).__name__)
    except KeyboardInterrupt:
        result.update(exit=130, reason="interrupted")
    finally:
        cleaned = True
        try:
            if job is not None:
                api.cleanup(job)
        except (OSError, GuardError):
            cleaned = False
        try:
            # If assignment failed, the only unassigned process is our suspended launcher.
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=CLEANUP_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            cleaned = False
        finally:
            if job is not None:
                api.k.CloseHandle(job)
            if parent is not None:
                for handle in parent if isinstance(parent, list) else [parent]:
                    api.k.CloseHandle(handle)
        result["cleanup"] = cleaned
        if not cleaned:
            result.update(exit=3, reason="cleanup unconfirmed")
        temporary = directory / "result.tmp"
        temporary.write_text(json.dumps(result, indent=2), encoding="utf-8")
        temporary.replace(directory / "result.json")
    print(f"Gate exit {result['exit']}; cleanup confirmed: {result['cleanup']}; "
          f"report: {directory}")
    return result["exit"]


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if os.name != "nt" or not command:
        parser.error("Windows and a command after -- are required")
    try:
        return run(command, args.report_dir)
    except (OSError, GuardError) as exc:
        print(f"Gate refused: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
