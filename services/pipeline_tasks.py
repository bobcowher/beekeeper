"""Inline shell tasks for pipeline steps: bash -c <command> in its own process group.

State is in-memory only. After a restart poll_task() reports "missing" and the
engine fails the step with "interrupted by restart".
"""
import datetime
import os
import signal
import subprocess
import threading
import time

KILL_GRACE_S = 5

_tasks: dict[int, dict] = {}   # step_pk -> {"proc", "started", "timeout_s", "log_path"}
_lock = threading.Lock()


def spawn_task(step_pk, command, cwd, env, log_path, timeout_s) -> int:
    os.makedirs(cwd, exist_ok=True)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        started = datetime.datetime.now().isoformat(timespec="seconds")
        os.write(fd, f"[beekeeper] Task started {started}\n[beekeeper] Command:\n{command}\n\n".encode())
        proc = subprocess.Popen(
            ["bash", "-c", command], cwd=cwd, env=env,
            stdout=fd, stderr=subprocess.STDOUT, start_new_session=True,
        )
    finally:
        os.close(fd)
    with _lock:
        _tasks[step_pk] = {"proc": proc, "started": time.time(),
                           "timeout_s": timeout_s, "log_path": log_path}
    return proc.pid


def _terminate(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        pass

    # The shell can exit on SIGTERM while a child in its process group ignores
    # it. Always make the final group-wide SIGKILL attempt before returning.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        pass


def _footer(info, code, outcome):
    duration = int(time.time() - info["started"])
    try:
        with open(info["log_path"], "a") as f:
            f.write(f"\n[beekeeper] Task {outcome.replace('_', ' ')}: exit code {code}, {duration}s\n")
    except OSError:
        pass


def poll_task(step_pk) -> tuple[str, int | None]:
    with _lock:
        info = _tasks.get(step_pk)
    if info is None:
        return "missing", None
    proc = info["proc"]
    code = proc.poll()
    if code is None:
        if time.time() - info["started"] <= info["timeout_s"]:
            return "running", None
        _terminate(proc)
        code, outcome = proc.returncode, "timed_out"
    else:
        outcome = "exited"
    _footer(info, code, outcome)
    with _lock:
        _tasks.pop(step_pk, None)
    return outcome, code


def kill_task(step_pk) -> int | None:
    with _lock:
        info = _tasks.pop(step_pk, None)
    if info is None:
        return None
    _terminate(info["proc"])
    _footer(info, info["proc"].returncode, "cancelled")
    return info["proc"].returncode


def running_task_pks() -> list[int]:
    with _lock:
        return list(_tasks)
