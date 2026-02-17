import os
import json
import signal
import socket
import subprocess
import threading
import time
import logging

from models.project import Project

log = logging.getLogger(__name__)

_running = {}
_tb_running = {}  # standalone TB processes: {name: {"tb_process": Popen, "tb_port": int, "last_access": float}}
_lock = threading.Lock()
_TB_IDLE_TIMEOUT = 1800  # 30 min


def _resolve_python_binary(projects_dir, project):
    """Get the python binary path for a project's environment."""
    if project.get("env_type") == "conda":
        return _resolve_conda_python(project)
    # venv — check python, python3, and versioned binary
    venv_bin = os.path.join(projects_dir, project["name"], "venv", "bin")
    for name in ("python", "python3", f"python{project.get('python_version', '')}"):
        candidate = os.path.join(venv_bin, name)
        if os.path.isfile(candidate):
            return candidate
    log.warning("No python binary found in %s", venv_bin)
    return None


def _resolve_conda_python(project):
    """Resolve python binary from a conda environment."""
    from services.python_versions import _find_conda_bin
    from services.project_service import _conda_env_name, _resolve_conda_env_path

    conda_bin = _find_conda_bin()
    if not conda_bin:
        return None
    env_name = _conda_env_name(project["name"])
    env_path = _resolve_conda_env_path(conda_bin, env_name)
    if not env_path:
        return None
    return os.path.join(env_path, "bin", "python")


def _resolve_tensorboard_binary(projects_dir, project):
    """Get the tensorboard binary path for a project's environment."""
    if project.get("env_type") == "conda":
        from services.python_versions import _find_conda_bin
        from services.project_service import _conda_env_name, _resolve_conda_env_path

        conda_bin = _find_conda_bin()
        if conda_bin:
            env_name = _conda_env_name(project["name"])
            env_path = _resolve_conda_env_path(conda_bin, env_name)
            if env_path:
                tb = os.path.join(env_path, "bin", "tensorboard")
                if os.path.isfile(tb):
                    return tb
        return None

    # venv
    tb = os.path.join(projects_dir, project["name"], "venv", "bin", "tensorboard")
    if os.path.isfile(tb):
        return tb
    return None


def _find_free_port(start=6006):
    """Find a free port starting from the given port."""
    for port in range(start, start + 100):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("", port))
                return port
        except OSError:
            continue
    return None


def _update_project_json(projects_dir, name, **fields):
    """Update specific fields in project.json atomically."""
    config_path = os.path.join(projects_dir, name, "project.json")
    with open(config_path) as f:
        data = json.load(f)
    data.update(fields)
    project = Project(**data)
    project.save(projects_dir)


def _kill_tb_process(tb_proc):
    """Kill a tensorboard process: SIGTERM -> 5s wait -> SIGKILL."""
    if tb_proc and tb_proc.poll() is None:
        try:
            tb_proc.terminate()
            tb_proc.wait(timeout=5)
        except Exception:
            try:
                tb_proc.kill()
            except Exception:
                pass


def _tb_idle_reaper():
    """Daemon thread that kills standalone TB processes idle for >30 min."""
    while True:
        time.sleep(60)
        now = time.time()
        to_kill = []
        with _lock:
            for name, info in list(_tb_running.items()):
                if now - info.get("last_access", now) > _TB_IDLE_TIMEOUT:
                    to_kill.append((name, info["tb_process"]))
                    del _tb_running[name]
        for name, proc in to_kill:
            log.info("Killing idle standalone TB for %s", name)
            _kill_tb_process(proc)


# Start the idle reaper thread
threading.Thread(target=_tb_idle_reaper, daemon=True).start()


def _monitor_process(projects_dir, name):
    """Background thread that waits for the training process to exit."""
    while True:
        with _lock:
            info = _running.get(name)
            if not info:
                return
            proc = info["process"]

        ret = proc.poll()
        if ret is not None:
            with _lock:
                info = _running.get(name)
                if info:
                    # Migrate tensorboard to standalone tracking
                    tb = info.get("tb_process")
                    tb_port = info.get("tb_port")
                    if tb and tb.poll() is None and tb_port:
                        _tb_running[name] = {
                            "tb_process": tb,
                            "tb_port": tb_port,
                            "last_access": time.time(),
                        }
                        log.info("Migrated TB for %s to standalone (port %d)", name, tb_port)
                    del _running[name]

            status = "stopped" if ret == 0 else "crashed"
            _update_project_json(projects_dir, name,
                                 train_status=status, train_pid=0)
            log.info("Training for %s exited with code %d (status: %s)",
                     name, ret, status)
            return

        time.sleep(1)


def start_training(projects_dir, name):
    """Start the training subprocess for a project."""
    with _lock:
        if name in _running:
            return {"error": "Training is already running"}

    config_path = os.path.join(projects_dir, name, "project.json")
    if not os.path.isfile(config_path):
        return {"error": "Project not found"}

    with open(config_path) as f:
        project = json.load(f)

    if project.get("setup_status") != "ready":
        return {"error": "Project setup is not complete"}

    python_bin = _resolve_python_binary(projects_dir, project)
    if not python_bin:
        if project.get("env_type") == "conda":
            hint = f"conda env beekeeper-{name}"
        else:
            hint = os.path.join(projects_dir, name, "venv", "bin")
        return {"error": f"Could not find Python binary (checked {hint})"}

    src_dir = os.path.join(projects_dir, name, "src")

    # Pull latest code before running
    branch = project.get("branch", "main")
    try:
        result = subprocess.run(
            ["git", "pull", "origin", branch],
            cwd=src_dir,
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return {"error": f"Git pull failed: {result.stderr.strip()[-500:]}"}
    except subprocess.TimeoutExpired:
        return {"error": "Git pull timed out (60s)"}
    except Exception as e:
        return {"error": f"Git pull failed: {e}"}

    train_file = project.get("train_file", "train.py")
    train_path = os.path.join(src_dir, train_file)

    if not os.path.isfile(train_path):
        return {"error": f"Training file not found: {train_file}"}

    # Open log file — truncate previous run's log on new start
    log_path = os.path.join(projects_dir, name, "train.log")
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)

    # Build environment: inherit system env + project-specific vars
    proc_env = os.environ.copy()
    proc_env.update(project.get("env_vars") or {})

    # Start training process
    try:
        proc = subprocess.Popen(
            [python_bin, "-u", train_file],
            cwd=src_dir,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            env=proc_env,
            start_new_session=True,
        )
    except Exception as e:
        os.close(log_fd)
        return {"error": f"Failed to start training: {e}"}

    # Close our copy of the fd — the child process has its own
    os.close(log_fd)

    # Kill any standalone TB before starting a new one with training
    with _lock:
        old_tb = _tb_running.pop(name, None)
    if old_tb:
        _kill_tb_process(old_tb["tb_process"])

    # Start tensorboard
    tb_process = None
    tb_port = None
    tb_bin = _resolve_tensorboard_binary(projects_dir, project)
    if tb_bin:
        tb_port = _find_free_port()
        if tb_port:
            tb_logdir = os.path.join(src_dir, project.get("tensorboard_log_dir", "runs"))
            try:
                tb_process = subprocess.Popen(
                    [tb_bin, "--logdir", tb_logdir, "--port", str(tb_port),
                     "--bind_all"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception as e:
                log.warning("Failed to start tensorboard for %s: %s", name, e)
                tb_port = None

    with _lock:
        _running[name] = {
            "process": proc,
            "log_path": log_path,
            "tb_process": tb_process,
            "tb_port": tb_port,
            "started_at": time.time(),
        }

    _update_project_json(projects_dir, name,
                         train_status="running", train_pid=proc.pid)

    # Start monitor thread
    thread = threading.Thread(
        target=_monitor_process, args=(projects_dir, name), daemon=True
    )
    thread.start()

    return {"status": "started", "pid": proc.pid, "tb_port": tb_port}


def stop_training(projects_dir, name):
    """Stop the training subprocess for a project."""
    with _lock:
        info = _running.get(name)
        if not info:
            return {"error": "Training is not running"}
        proc = info["process"]

    # SIGTERM the process group (the child is session leader)
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass

    # Wait up to 5 seconds for graceful shutdown
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
            proc.wait(timeout=3)
        except (ProcessLookupError, OSError):
            pass

    with _lock:
        info = _running.pop(name, None)
        if info:
            # Migrate tensorboard to standalone tracking
            tb = info.get("tb_process")
            tb_port = info.get("tb_port")
            if tb and tb.poll() is None and tb_port:
                _tb_running[name] = {
                    "tb_process": tb,
                    "tb_port": tb_port,
                    "last_access": time.time(),
                }
                log.info("Migrated TB for %s to standalone (port %d)", name, tb_port)

    _update_project_json(projects_dir, name,
                         train_status="stopped", train_pid=0)

    return {"status": "stopped"}


def get_training_status(name):
    """Get the current training status for a project."""
    with _lock:
        info = _running.get(name)
        if info:
            proc = info["process"]
            return {
                "status": "running",
                "pid": proc.pid,
                "started_at": info.get("started_at"),
                "tb_port": info.get("tb_port"),
                "elapsed": time.time() - info.get("started_at", time.time()),
            }
    # Check standalone TB
    with _lock:
        tb_info = _tb_running.get(name)
        if tb_info:
            tb_info["last_access"] = time.time()
            tb_port = tb_info.get("tb_port")
        else:
            tb_port = None
    return {
        "status": "idle",
        "pid": None,
        "started_at": None,
        "tb_port": tb_port,
        "elapsed": None,
    }


def start_tensorboard(projects_dir, name):
    """Start tensorboard on-demand for a project. Returns existing port if already running."""
    # Check if TB is already running (from training or standalone)
    with _lock:
        info = _running.get(name)
        if info and info.get("tb_port"):
            tb = info.get("tb_process")
            if tb and tb.poll() is None:
                return {"tb_port": info["tb_port"]}
        tb_info = _tb_running.get(name)
        if tb_info:
            tb = tb_info.get("tb_process")
            if tb and tb.poll() is None:
                tb_info["last_access"] = time.time()
                return {"tb_port": tb_info["tb_port"]}
            else:
                del _tb_running[name]

    config_path = os.path.join(projects_dir, name, "project.json")
    if not os.path.isfile(config_path):
        return {"error": "Project not found"}

    with open(config_path) as f:
        project = json.load(f)

    tb_bin = _resolve_tensorboard_binary(projects_dir, project)
    if not tb_bin:
        return {"error": "Tensorboard not found in project environment"}

    src_dir = os.path.join(projects_dir, name, "src")
    tb_logdir = os.path.join(src_dir, project.get("tensorboard_log_dir", "runs"))
    if not os.path.isdir(tb_logdir):
        return {"error": f"Tensorboard log directory not found: {project.get('tensorboard_log_dir', 'runs')}"}

    tb_port = _find_free_port()
    if not tb_port:
        return {"error": "No free port available for Tensorboard"}

    try:
        tb_process = subprocess.Popen(
            [tb_bin, "--logdir", tb_logdir, "--port", str(tb_port), "--bind_all"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        return {"error": f"Failed to start Tensorboard: {e}"}

    with _lock:
        _tb_running[name] = {
            "tb_process": tb_process,
            "tb_port": tb_port,
            "last_access": time.time(),
        }

    log.info("Started standalone TB for %s on port %d", name, tb_port)
    return {"tb_port": tb_port}


def stop_tensorboard(name):
    """Stop standalone tensorboard for a project."""
    with _lock:
        tb_info = _tb_running.pop(name, None)
    if tb_info:
        _kill_tb_process(tb_info["tb_process"])
        log.info("Stopped standalone TB for %s", name)
        return {"status": "stopped"}
    return {"status": "not_running"}
