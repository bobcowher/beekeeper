import datetime
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
            log_path = None
            started_at = None
            run_id = None
            with _lock:
                info = _running.get(name)
                if info:
                    log_path = info.get("log_path")
                    started_at = info.get("started_at")
                    run_id = info.get("run_id")
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

            if log_path and started_at:
                _append_run_footer(log_path, ret, started_at)

            # Archive log and finalize run record
            archived_log_path = None
            if run_id and log_path and os.path.isfile(log_path):
                archived_log_path = _archive_run_log(projects_dir, name, run_id, log_path)

            if run_id and started_at:
                _finalize_run_record(run_id, ret, started_at, archived_log_path)

            # Prune old runs (keep last 20)
            _prune_old_runs(projects_dir, name, keep_last=20)

            status = "stopped" if ret == 0 else "crashed"
            _update_project_json(projects_dir, name,
                                 train_status=status, train_pid=0)
            log.info("Training for %s exited with code %d (status: %s)",
                     name, ret, status)
            return

        time.sleep(1)


def _collect_run_metadata(workspace_dir, python_bin, branch):
    """Collect environment metadata for the run header."""
    meta = {
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "hostname": socket.gethostname(),
        "branch": branch,
        "commit_sha": "unknown",
        "commit_msg": "",
        "python_version": "unknown",
        "gpu_info": [],
    }

    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace_dir,
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            meta["commit_sha"] = r.stdout.strip()
    except Exception:
        pass

    try:
        r = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=workspace_dir,
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            meta["commit_msg"] = r.stdout.strip()
    except Exception:
        pass

    try:
        r = subprocess.run([python_bin, "--version"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            meta["python_version"] = (r.stdout or r.stderr).strip()
    except Exception:
        pass

    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            for line in r.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) == 2:
                    meta["gpu_info"].append(f"{parts[0]} ({int(parts[1]):,} MiB)")
                else:
                    meta["gpu_info"].append(line.strip())
    except Exception:
        pass

    return meta


def _write_run_header(log_fd, meta, project):
    """Write a banner to the log file at the start of a run."""
    sep = "=" * 60
    lines = [
        sep,
        "  BEEKEEPER RUN START",
        f"  {meta['timestamp']}  |  host: {meta['hostname']}",
        f"  project : {project['name']}",
        f"  branch  : {meta['branch']}",
        f"  commit  : {meta['commit_sha'][:12]}  {meta['commit_msg']}",
        f"  python  : {meta['python_version']}",
        f"  script  : {project.get('train_file', 'train.py')}",
    ]
    for i, g in enumerate(meta["gpu_info"]):
        label = "  gpu     :" if i == 0 else "           "
        lines.append(f"{label} {g}")
    lines += [sep, ""]
    os.write(log_fd, ("\n".join(lines) + "\n").encode())


def _append_run_footer(log_path, ret, started_at):
    """Append an exit banner to the log file at the end of a run."""
    elapsed = time.time() - started_at
    h, rem = divmod(int(elapsed), 3600)
    m, s = divmod(rem, 60)
    elapsed_str = f"{h:02d}:{m:02d}:{s:02d}"
    status = "COMPLETED" if ret == 0 else f"CRASHED (exit {ret})"
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sep = "=" * 60
    lines = ["", sep, "  BEEKEEPER RUN END",
             f"  {timestamp}  |  elapsed: {elapsed_str}",
             f"  status  : {status}", sep, ""]
    try:
        with open(log_path, "a") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


def _archive_run_log(projects_dir: str, project_name: str, run_id: int, source_log_path: str) -> str:
    """
    Copy train.log to archived location.
    Returns relative path (e.g., "run_logs/run-20260321-171532-0042.log").
    """
    import shutil

    # Create archive directory
    archive_dir = os.path.join(projects_dir, project_name, "run_logs")
    os.makedirs(archive_dir, exist_ok=True)

    # Generate filename: run-{YYYYMMDD-HHMMSS}-{id}.log
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"run-{timestamp}-{run_id:04d}.log"
    dest_path = os.path.join(archive_dir, filename)

    # Copy log file atomically
    try:
        shutil.copy2(source_log_path, dest_path)
        return f"run_logs/{filename}"
    except Exception as e:
        log.error(f"Failed to archive log for run {run_id}: {e}")
        return None


def _finalize_run_record(run_id: int, exit_code: int, started_at: float, log_path: str):
    """Update run record with completion data."""
    from services.db_service import get_db

    ended_at = datetime.datetime.now()
    duration = int(time.time() - started_at)

    # Determine status from exit code
    if exit_code == 0:
        status = 'completed'
    elif exit_code < 0:  # Killed by signal (SIGTERM=-15, SIGKILL=-9)
        status = 'canceled'
    else:
        status = 'crashed'

    get_db().update_training_run(
        run_id,
        ended_at=ended_at,
        duration_seconds=duration,
        status=status,
        exit_code=exit_code,
        log_file_path=log_path
    )


def _prune_old_runs(projects_dir: str, project_name: str, keep_last: int = 20):
    """
    Delete runs beyond retention limit.
    Removes: DB records, archived log files, Tensorboard directories.
    """
    from services.db_service import get_db
    import shutil

    db = get_db()
    deleted_runs = db.prune_old_runs(project_name, keep_last=keep_last)

    for run in deleted_runs:
        # Delete archived log file
        if run.get('log_file_path'):
            log_path = os.path.join(projects_dir, project_name, run['log_file_path'])
            try:
                if os.path.isfile(log_path):
                    os.unlink(log_path)
                    log.info(f"Deleted archived log: {log_path}")
            except Exception as e:
                log.warning(f"Failed to delete archived log {log_path}: {e}")

        # Delete Tensorboard directory
        if run.get('tensorboard_dir'):
            tb_path = os.path.join(projects_dir, project_name, run['tensorboard_dir'])
            try:
                if os.path.isdir(tb_path):
                    shutil.rmtree(tb_path)
                    log.info(f"Deleted Tensorboard logs: {tb_path}")
            except Exception as e:
                log.warning(f"Failed to delete Tensorboard logs {tb_path}: {e}")


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

    workspace_dir = os.path.join(projects_dir, name, "workspace")

    # Pull latest code before running
    branch = project.get("branch", "main")
    try:
        result = subprocess.run(
            ["git", "pull", "origin", branch],
            cwd=workspace_dir,
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return {"error": f"Git pull failed: {result.stderr.strip()[-500:]}"}
    except subprocess.TimeoutExpired:
        return {"error": "Git pull timed out (60s)"}
    except Exception as e:
        return {"error": f"Git pull failed: {e}"}

    run_meta = _collect_run_metadata(workspace_dir, python_bin, branch)

    # Create run record in database
    from services.db_service import get_db
    run_id = get_db().create_training_run(
        project_name=name,
        metadata={
            'started_at': datetime.datetime.now(),
            'status': 'running',
            'commit_sha': run_meta['commit_sha'],
            'commit_message': run_meta['commit_msg'],
            'branch': branch,
            'python_version': run_meta['python_version'],
            'gpu_info': json.dumps(run_meta['gpu_info']),
            'hostname': run_meta['hostname'],
        }
    )

    # Ensure data dir symlink exists (before setup script so setup.sh can use it)
    if project.get("data_dir_enabled") and project.get("data_dir_remote"):
        data_dir_remote = project["data_dir_remote"]
        data_dir_local = project.get("data_dir_local", "data")
        local_path = os.path.join(workspace_dir, data_dir_local)
        if os.path.islink(local_path):
            if os.readlink(local_path) != data_dir_remote:
                os.unlink(local_path)
                os.symlink(data_dir_remote, local_path)
        elif os.path.exists(local_path):
            return {
                "error": f"'{data_dir_local}' already exists in the repository and is not a symlink. "
                         f"Remove it from the repo or disable the data directory in project settings."
            }
        elif os.path.isdir(data_dir_remote):
            os.symlink(data_dir_remote, local_path)
        else:
            return {"error": f"Data directory '{data_dir_remote}' does not exist on this server."}

    # Run setup script if configured and present
    setup_script = project.get("setup_script", "")
    if setup_script:
        script_path = os.path.join(workspace_dir, setup_script)
        if os.path.isfile(script_path):
            try:
                result = subprocess.run(
                    ["bash", script_path],
                    cwd=workspace_dir,
                    capture_output=True, text=True, timeout=300,
                )
                if result.returncode != 0:
                    return {"error": f"Setup script failed: {result.stderr.strip()[-500:]}"}
            except subprocess.TimeoutExpired:
                return {"error": "Setup script timed out (300s)"}
            except Exception as e:
                return {"error": f"Setup script failed: {e}"}

    # Install/update dependencies so newly added packages are always present
    req_file = project.get("requirements_file", "requirements.txt")
    req_path = os.path.join(workspace_dir, req_file)
    if os.path.isfile(req_path):
        try:
            result = subprocess.run(
                [python_bin, "-m", "pip", "install", "-r", req_path, "--quiet"],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode != 0:
                return {"error": f"Pip install failed: {result.stderr.strip()[-500:]}"}
        except subprocess.TimeoutExpired:
            return {"error": "Pip install timed out (300s)"}
        except Exception as e:
            return {"error": f"Pip install failed: {e}"}

    train_file = project.get("train_file", "train.py")
    train_path = os.path.join(workspace_dir, train_file)

    if not os.path.isfile(train_path):
        return {"error": f"Training file not found: {train_file}"}

    # Open log file — truncate previous run's log on new start
    log_path = os.path.join(projects_dir, name, "train.log")
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    _write_run_header(log_fd, run_meta, project)

    # Build environment: inherit system env + project-specific vars
    proc_env = os.environ.copy()
    proc_env.update(project.get("env_vars") or {})

    # Start training process
    try:
        proc = subprocess.Popen(
            [python_bin, "-u", train_file],
            cwd=workspace_dir,
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

    # Start tensorboard with timestamped directory
    tb_process = None
    tb_port = None
    tb_run_dir_rel = None
    tb_bin = _resolve_tensorboard_binary(projects_dir, project)
    if tb_bin:
        tb_port = _find_free_port()
        if tb_port:
            tb_logdir_base = os.path.join(workspace_dir, project.get("tensorboard_log_dir", "runs"))

            # Create timestamped subdirectory for this run
            run_timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            tb_run_dir = os.path.join(tb_logdir_base, run_timestamp)
            os.makedirs(tb_run_dir, exist_ok=True)
            tb_run_dir_rel = f"{project.get('tensorboard_log_dir', 'runs')}/{run_timestamp}"

            # Launch Tensorboard pointing to the base directory (shows all runs)
            try:
                tb_process = subprocess.Popen(
                    [tb_bin, "--logdir", tb_logdir_base, "--port", str(tb_port),
                     "--bind_all"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )

                # Update run record with Tensorboard directory
                get_db().update_training_run(run_id, tensorboard_dir=tb_run_dir_rel)
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
            "run_id": run_id,
        }

    _update_project_json(projects_dir, name,
                         train_status="running", train_pid=proc.pid,
                         last_run_at=time.time())

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

    workspace_dir = os.path.join(projects_dir, name, "workspace")
    tb_logdir = os.path.join(workspace_dir, project.get("tensorboard_log_dir", "runs"))

    # Create the log directory if it doesn't exist
    if not os.path.isdir(tb_logdir):
        try:
            os.makedirs(tb_logdir, exist_ok=True)
        except Exception as e:
            return {"error": f"Failed to create Tensorboard log directory: {e}"}

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
