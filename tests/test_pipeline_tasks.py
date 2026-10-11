import os
import time

from services import pipeline_tasks as tasks


def wait_done(pk, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        outcome, code = tasks.poll_task(pk)
        if outcome != "running":
            return outcome, code
        time.sleep(0.05)
    raise AssertionError("task did not finish")


def env_with(**kw):
    env = dict(os.environ)
    env.update(kw)
    return env


def test_exit_zero_and_log(tmp_path):
    log = tmp_path / "s.log"
    tasks.spawn_task(1, 'echo "dir=$BEEKEEPER_STEP_A_DIR"', str(tmp_path / "cwd"),
                     env_with(BEEKEEPER_STEP_A_DIR="/x"), str(log), 60)
    assert wait_done(1) == ("exited", 0)
    text = log.read_text()
    assert "[beekeeper] Command:" in text and "dir=/x" in text
    assert "Task exited: exit code 0" in text
    assert tasks.running_task_pks() == []


def test_cwd_is_created_and_used(tmp_path):
    cwd = tmp_path / "run"
    tasks.spawn_task(2, "pwd", str(cwd), env_with(), str(tmp_path / "l.log"), 60)
    wait_done(2)
    assert str(cwd) in (tmp_path / "l.log").read_text()


def test_nonzero_exit(tmp_path):
    tasks.spawn_task(3, "exit 3", str(tmp_path), env_with(), str(tmp_path / "l.log"), 60)
    assert wait_done(3) == ("exited", 3)


def test_timeout_kills_process_group(tmp_path):
    tasks.spawn_task(4, "sleep 30 & sleep 30", str(tmp_path), env_with(), str(tmp_path / "l.log"), 1)
    time.sleep(1.2)
    outcome, code = wait_done(4)
    assert outcome == "timed_out" and code != 0
    assert "Task timed out" in (tmp_path / "l.log").read_text()


def test_kill_task(tmp_path):
    pid = tasks.spawn_task(5, "sleep 30", str(tmp_path), env_with(), str(tmp_path / "l.log"), 60)
    tasks.kill_task(5)
    assert tasks.poll_task(5) == ("missing", None)
    deadline = time.time() + 2
    while time.time() < deadline:
        try:
            if open(f"/proc/{pid}/stat").read().split()[2] == "Z":
                break
        except FileNotFoundError:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("process still alive")


def test_kill_task_kills_child_that_ignores_term_after_shell_exits(tmp_path):
    child_pid_file = tmp_path / "child.pid"
    command = (
        "trap '' TERM; sleep 30 & child=$!; trap - TERM; "
        'printf "%s" "$child" > "$CHILD_PID_FILE"; wait'
    )
    tasks.spawn_task(6, command, str(tmp_path),
                     env_with(CHILD_PID_FILE=str(child_pid_file)), str(tmp_path / "l.log"), 60)
    deadline = time.time() + 2
    while not child_pid_file.exists() and time.time() < deadline:
        time.sleep(0.02)
    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text())

    tasks.kill_task(6)

    deadline = time.time() + 2
    while time.time() < deadline:
        try:
            if open(f"/proc/{child_pid}/stat").read().split()[2] == "Z":
                break
        except FileNotFoundError:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("SIGTERM-ignoring task child still alive")


def test_unknown_task_is_missing():
    assert tasks.poll_task(999) == ("missing", None)


def test_task_exit_wakes_dispatcher(tmp_path):
    from services import dispatcher
    dispatcher._wake.clear()
    tasks.spawn_task(90, "true", str(tmp_path), env_with(), str(tmp_path / "l.log"), 60)
    assert dispatcher._wake.wait(timeout=5)
    assert wait_done(90) == ("exited", 0)
