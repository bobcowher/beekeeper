import datetime
import os
import json
import signal
import socket
import subprocess
import threading
import time
import logging
import shutil

from models.project import Project
from services.db_service import get_db
from services.project_service import ensure_data_dir_symlink, validate_output_paths, validate_workspace_path
from services.run_storage_service import delete_run_storage, persistent_runs_root

log = logging.getLogger(__name__)

_PROJECT_FILE = "project.json"

_running = {}  # {run_id: info_dict} — keyed by DB run ID (int)
_tb_running = {}  # standalone TB processes: {name: {"tb_process": Popen, "tb_port": int, "last_access": float}}
_lock = threading.Lock()
_TB_IDLE_TIMEOUT = 1800  # 30 min
_BEEKEEPER_RUN_ENV_KEYS = {
    "BEEKEEPER_RUN_DIR",
    "BEEKEEPER_TENSORBOARD_DIR",
    "TENSORBOARD_LOG_DIR",
}


def _persistent_run_paths(projects_dir: str, name: str, run_id: int) -> tuple[str, str]:
    rel = f"persistent/runs/run_{run_id}"
    return rel, os.path.join(projects_dir, name, rel)  # NOSONAR


def _dir_has_entries(path: str) -> bool:
    try:
        if not os.path.isdir(path):
            return False
        with os.scandir(path) as entries:
            return any(entries)
    except OSError:
        return False


def _tensorboard_launch_args(tb_bin: str, projects_dir: str, name: str, project: dict, port: int) -> list[str]:
    persistent_root = persistent_runs_root(projects_dir, name)
    os.makedirs(persistent_root, exist_ok=True)

    args = [tb_bin]
    tb_rel = validate_workspace_path(project.get("tensorboard_log_dir") or "runs", "TensorBoard log dir")
    legacy_root = os.path.join(projects_dir, name, "workspace", tb_rel)
    if not os.path.islink(legacy_root) and _dir_has_entries(legacy_root):
        args.extend(["--logdir_spec", f"persistent:{persistent_root},legacy:{legacy_root}"])
    else:
        args.extend(["--logdir", persistent_root])
    args.extend(["--port", str(port), "--bind_all", "--reload_interval=5"])
    return args


def _ensure_workspace_symlink(  # NOSONAR — sequential symlink setup, complexity is inherent
    workspace_dir: str,
    rel_path: str,
    target_dir: str,
    warnings: list[str],
    label: str,
    is_parallel: bool = False,
):
    link_path = os.path.join(workspace_dir, rel_path)
    os.makedirs(target_dir, exist_ok=True)
    try:
        os.makedirs(os.path.dirname(link_path), exist_ok=True)
    except Exception as e:
        warnings.append(f"Could not create parent directory for {label} symlink '{rel_path}': {e}")
        return

    if os.path.lexists(link_path):
        if os.path.islink(link_path):
            if os.path.realpath(link_path) == os.path.realpath(target_dir):
                return
            try:
                os.unlink(link_path)
            except Exception as e:
                warnings.append(f"Could not replace existing {label} symlink '{rel_path}': {e}")
                return
        elif os.path.isdir(link_path):
            try:
                if is_parallel:
                    shutil.rmtree(link_path)
                else:
                    os.rmdir(link_path)
            except OSError:
                warnings.append(
                    f"Could not protect {label} path '{rel_path}' because a non-empty directory already exists there."
                )
                return
        else:
            if is_parallel:
                try:
                    os.unlink(link_path)
                except Exception as e:
                    warnings.append(f"Could not replace existing {label} file '{rel_path}': {e}")
                    return
            else:
                warnings.append(
                    f"Could not protect {label} path '{rel_path}' because a file already exists there."
                )
                return

    try:
        os.symlink(target_dir, link_path)
    except Exception as e:
        warnings.append(f"Could not create {label} symlink '{rel_path}': {e}")


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
    config_path = os.path.join(projects_dir, name, _PROJECT_FILE)  # NOSONAR
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


def _get_runs_for_project(name: str) -> list:
    """Internal: return all active info dicts for a project (holds _lock NOT required — callers handle locking)."""
    return [info for info in _running.values() if info.get("project_name") == name]


def get_runs_for_project(name: str) -> list:
    """Return active run summary dicts for a project. Safe for API/MCP consumers."""
    now = time.time()
    result = []
    with _lock:
        for run_id, info in _running.items():
            if info.get("project_name") != name:
                continue
            if info.get("starting"):
                result.append({
                    "run_id": run_id,
                    "branch": info.get("branch", ""),
                    "status": "starting",
                    "pid": None,
                    "elapsed": None,
                    "tb_port": None,
                    "resources": None,
                })
            else:
                proc = info.get("process")
                pid = proc.pid if proc else None
                from services.resource_tracker import get_process_resources
                resources = get_process_resources(pid) if pid else None
                result.append({
                    "run_id": run_id,
                    "branch": info.get("branch", ""),
                    "status": "running",
                    "pid": pid,
                    "elapsed": now - info.get("started_at", now),
                    "tb_port": info.get("tb_port"),
                    "resources": resources,
                })
    return result


def get_run_log_path(projects_dir: str, name: str, run_id: int | None = None) -> str:
    """Return the active log file path for a run. Falls back to train.log."""
    with _lock:
        if run_id is not None:
            info = _running.get(run_id)
            if info and info.get("log_path"):
                return info["log_path"]
        else:
            active = [
                info for info in _running.values()
                if info.get("project_name") == name and not info.get("starting")
            ]
            if len(active) == 1 and active[0].get("log_path"):
                return active[0]["log_path"]
    return os.path.join(projects_dir, name, "train.log")


def _monitor_process(projects_dir, name, run_id):  # NOSONAR — sequential cleanup pipeline
    """Background thread that waits for the training process to exit."""
    while True:
        with _lock:
            info = _running.get(run_id)
            if not info:
                return
            proc = info["process"]

        ret = proc.poll()
        if ret is not None:
            log_path = None
            started_at = None
            with _lock:
                info = _running.get(run_id)
                if info:
                    log_path = info.get("log_path")
                    started_at = info.get("started_at")
                    tb = info.get("tb_process")
                    tb_port = info.get("tb_port")
                    if tb and tb.poll() is None and tb_port:
                        _tb_running[name] = {
                            "tb_process": tb,
                            "tb_port": tb_port,
                            "last_access": time.time(),
                        }
                        log.info("Migrated TB for %s run %d to standalone (port %d)", name, run_id, tb_port)
                    del _running[run_id]

            workspace_dir = info.get("workspace_dir") if info else None
            primary_ws = os.path.join(projects_dir, name, "workspace")
            is_parallel = workspace_dir and workspace_dir != primary_ws

            if log_path and started_at:
                _append_run_footer(log_path, ret, started_at)

            archived_log_path = None
            if log_path and os.path.isfile(log_path):
                archived_log_path = _archive_run_log(projects_dir, name, run_id, log_path)

            if started_at:
                _finalize_run_record(run_id, ret, started_at, archived_log_path)

            # Clean up parallel workspace on natural completion
            if is_parallel and workspace_dir:
                try:
                    shutil.rmtree(workspace_dir)
                    log.info("Deleted parallel workspace %s", workspace_dir)
                except Exception as e:
                    log.warning("Failed to delete parallel workspace %s: %s", workspace_dir, e)

            if ret == 0:
                import threading as _threading
                from services import tensorboard_service
                _threading.Thread(
                    target=tensorboard_service.parse_run_metrics,
                    args=(projects_dir, name, run_id),
                    daemon=True,
                ).start()

            _prune_old_runs(projects_dir, name, keep_last=20)

            # Only update train_status when the last run for this project finishes
            with _lock:
                remaining = _get_runs_for_project(name)
            if not remaining:
                status = "stopped" if ret == 0 else "crashed"
                _update_project_json(projects_dir, name, train_status=status, train_pid=0)
                log.info("All runs for %s finished. Status: %s", name, status)
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


def _archive_run_log(projects_dir: str, project_name: str, run_id: int, source_log_path: str) -> str | None:
    """
    Copy train.log to archived location.
    Returns relative path (e.g., "run_logs/run-20260321-171532-0042.log").
    """
    import shutil

    # Create archive directory
    archive_dir = os.path.join(projects_dir, project_name, "run_logs")  # NOSONAR
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


def _finalize_run_record(run_id: int, exit_code: int, started_at: float, log_path: str | None):
    """Update run record with completion data."""
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
    db = get_db()
    deleted_runs = db.prune_old_runs(project_name, keep_last=keep_last)

    for run in deleted_runs:
        delete_run_storage(projects_dir, project_name, run)


def start_training(projects_dir, name, branch=None):  # NOSONAR — sequential pre-launch pipeline
    """Validate, reserve a training slot, and launch the pre-launch sequence in background."""
    config_path = os.path.join(projects_dir, name, _PROJECT_FILE)  # NOSONAR
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

    if branch is None:
        branch = project.get("branch", "main")

    # Pre-create DB record so run_id is available immediately for slot reservation
    run_id = get_db().create_training_run(
        project_name=name,
        metadata={
            "started_at": datetime.datetime.now(),
            "status": "starting",
            "branch": branch,
        }
    )

    primary_ws = os.path.join(projects_dir, name, "workspace")

    with _lock:
        active_runs = _get_runs_for_project(name)
        parallel_enabled = project.get("parallel_runs_enabled", False)
        max_runs = project.get("max_parallel_runs", 2)

        if not parallel_enabled and len(active_runs) > 0:
            get_db().delete_training_run(run_id)
            return {"error": "Training is already running. To run multiple runs simultaneously, enable Parallel Runs in Edit Project."}

        if parallel_enabled and len(active_runs) >= max_runs:
            get_db().delete_training_run(run_id)
            return {"error": f"At capacity ({max_runs} parallel runs)"}

        primary_in_use = any(info.get("workspace_dir") == primary_ws for info in active_runs)
        workspace_dir = (
            primary_ws if not primary_in_use
            else os.path.join(projects_dir, name, f"workspace-{run_id}")
        )

        _running[run_id] = {
            "process": None,
            "starting": True,
            "project_name": name,
            "run_id": run_id,
            "branch": branch,
            "workspace_dir": workspace_dir,
        }

    _update_project_json(projects_dir, name, train_status="starting")

    thread = threading.Thread(
        target=_execute_training,
        args=(projects_dir, name, project, python_bin, run_id, branch, workspace_dir),
        daemon=True,
    )
    thread.start()

    return {"run_id": run_id, "status": "starting"}


def _execute_training(projects_dir, name, project, python_bin, run_id, branch, workspace_dir):  # NOSONAR — sequential pre-launch pipeline
    """Run the full pre-launch sequence and start the training subprocess (runs in background thread)."""
    is_parallel = workspace_dir != os.path.join(projects_dir, name, "workspace")
    log_path = (
        os.path.join(projects_dir, name, f"train-{run_id}.log")
        if is_parallel
        else os.path.join(projects_dir, name, "train.log")
    )

    def _abort(msg):
        log.error("Pre-launch failed for %s run %d: %s", name, run_id, msg)
        try:
            with open(log_path, "w") as lf:
                lf.write(f"[beekeeper] Pre-launch failed: {msg}\n")
        except Exception:
            pass
        get_db().update_training_run(run_id, status="crashed")
        with _lock:
            _running.pop(run_id, None)
            remaining_after = _get_runs_for_project(name)
        if not remaining_after:
            _update_project_json(projects_dir, name, train_status="stopped")
        if is_parallel and os.path.isdir(workspace_dir):
            import shutil
            try:
                shutil.rmtree(workspace_dir)
            except Exception:
                pass

    # For parallel runs: clone fresh workspace
    if is_parallel:
        git_url = project.get("git_url", "")
        try:
            result = subprocess.run(
                ["git", "clone", git_url, workspace_dir],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode != 0:
                return _abort(f"Git clone failed: {result.stderr.strip()[-500:]}")
        except subprocess.TimeoutExpired:
            return _abort("Git clone timed out (300s)")
        except Exception as e:
            return _abort(f"Git clone failed: {e}")

    # Sync to remote — remote is always authoritative
    try:
        fetch = subprocess.run(
            ["git", "fetch", "origin"],
            cwd=workspace_dir,
            capture_output=True, text=True, timeout=60,
        )
        if fetch.returncode != 0:
            return _abort(f"Git fetch failed: {fetch.stderr.strip()[-500:]}")
        reset = subprocess.run(
            ["git", "reset", "--hard", f"origin/{branch}"],
            cwd=workspace_dir,
            capture_output=True, text=True, timeout=30,
        )
        if reset.returncode != 0:
            return _abort(f"Git reset failed: {reset.stderr.strip()[-500:]}")
    except subprocess.TimeoutExpired:
        return _abort("Git sync timed out (60s)")
    except Exception as e:
        return _abort(f"Git sync failed: {e}")

    run_meta = _collect_run_metadata(workspace_dir, python_bin, branch)

    # Update the pre-created DB record with full metadata
    get_db().update_training_run(
        run_id,
        status="running",
        commit_sha=run_meta["commit_sha"],
        commit_message=run_meta["commit_msg"],
        python_version=run_meta["python_version"],
        gpu_info=json.dumps(run_meta["gpu_info"]),
        hostname=run_meta["hostname"],
    )

    # Ensure data dir symlink (before setup script so it can use it)
    if project.get("data_dir_enabled") and project.get("data_dir_remote"):
        err = ensure_data_dir_symlink(
            workspace_dir,
            project.get("data_dir_local", "data"),
            project["data_dir_remote"],
        )
        if err:
            return _abort(err)

    # Run setup script if configured
    setup_script = project.get("setup_script", "")
    if setup_script:
        script_path = os.path.join(workspace_dir, setup_script)
        if os.path.isfile(script_path):
            try:
                if project.get("env_type") == "conda":
                    from services.python_versions import _find_conda_bin
                    conda_bin = _find_conda_bin()
                    env_name = f"beekeeper-{name}"
                    cmd = [conda_bin, "run", "-n", env_name, "bash", script_path]
                    env = None
                else:
                    venv_path = os.path.join(projects_dir, name, "venv")
                    cmd = ["bash", script_path]
                    env = os.environ.copy()
                    env["VIRTUAL_ENV"] = venv_path
                    env["PATH"] = f"{venv_path}/bin:{env.get('PATH', '')}"

                result = subprocess.run(
                    cmd, cwd=workspace_dir, env=env,
                    capture_output=True, text=True, timeout=300,
                )
                if result.returncode != 0:
                    return _abort(f"Setup script failed: {result.stderr.strip()[-500:]}")
            except subprocess.TimeoutExpired:
                return _abort("Setup script timed out (300s)")
            except Exception as e:
                return _abort(f"Setup script failed: {e}")

    # Install/update dependencies
    req_file = project.get("requirements_file", "requirements.txt")
    req_path = os.path.join(workspace_dir, req_file)
    if os.path.isfile(req_path):
        try:
            result = subprocess.run(
                [python_bin, "-m", "pip", "install", "-r", req_path, "--quiet"],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode != 0:
                return _abort(f"Pip install failed: {result.stderr.strip()[-500:]}")
        except subprocess.TimeoutExpired:
            return _abort("Pip install timed out (300s)")
        except Exception as e:
            return _abort(f"Pip install failed: {e}")

    train_file = project.get("train_file", "train.py")
    train_path = os.path.join(workspace_dir, train_file)
    if not os.path.isfile(train_path):
        return _abort(f"Training file not found: {train_file}")

    # Prepare persistent run storage before training starts. Fast scripts often
    # create SummaryWriter/log files immediately, so workspace redirects must
    # exist before Popen.
    tb_process = None
    tb_port = None
    prelaunch_warnings = []

    try:
        tb_log_rel = validate_workspace_path(
            project.get("tensorboard_log_dir") or "runs",
            "TensorBoard log dir",
        )
        output_paths = validate_output_paths(project.get("output_paths") or [], tb_log_rel)
    except ValueError as e:
        return _abort(str(e))

    persistent_run_rel, persistent_run_dir = _persistent_run_paths(projects_dir, name, run_id)
    os.makedirs(persistent_run_dir, exist_ok=True)

    _ensure_workspace_symlink(
        workspace_dir,
        tb_log_rel,
        persistent_run_dir,
        prelaunch_warnings,
        "TensorBoard",
        is_parallel=is_parallel,
    )
    for output_rel in output_paths:
        _ensure_workspace_symlink(
            workspace_dir,
            output_rel,
            os.path.join(persistent_run_dir, output_rel),
            prelaunch_warnings,
            "output",
            is_parallel=is_parallel,
        )

    get_db().update_training_run(
        run_id,
        persistent_dir=persistent_run_rel,
        tensorboard_dir=persistent_run_rel,
    )

    run_history_max_runs = project.get("run_history_max_runs", 10)
    if run_history_max_runs > 0:
        runs = get_db().get_training_runs(name, limit=1000)
        runs.sort(key=lambda r: r["started_at"], reverse=True)
        deleted_count = 0
        for r in runs[run_history_max_runs:]:
            if not r.get("notable", 0):
                delete_run_storage(projects_dir, name, r)
                get_db().delete_training_run(r["id"])
                deleted_count += 1
        if deleted_count > 0:
            log.info(f"Auto-cleanup: deleted {deleted_count} old run(s)")

    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    _write_run_header(log_fd, run_meta, project)
    for warning in prelaunch_warnings:
        os.write(log_fd, f"[beekeeper] WARNING: {warning}\n".encode())
    if prelaunch_warnings:
        os.write(log_fd, b"\n")

    proc_env = os.environ.copy()
    project_env = project.get("env_vars") or {}
    for key in sorted(_BEEKEEPER_RUN_ENV_KEYS & set(project_env)):
        warning = f"Project env var {key} is reserved by Beekeeper and was overridden for this run."
        prelaunch_warnings.append(warning)
        os.write(log_fd, f"[beekeeper] WARNING: {warning}\n".encode())
    proc_env.update(project_env)
    proc_env["BEEKEEPER_RUN_DIR"] = persistent_run_dir
    proc_env["BEEKEEPER_TENSORBOARD_DIR"] = persistent_run_dir
    proc_env["TENSORBOARD_LOG_DIR"] = persistent_run_dir

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
        return _abort(f"Failed to start training: {e}")

    os.close(log_fd)

    # Kill any standalone TB for this project before starting a new one
    with _lock:
        old_tb = _tb_running.pop(name, None)
    if old_tb:
        _kill_tb_process(old_tb["tb_process"])

    # Start TensorBoard against persistent/runs so all active and historical
    # persistent runs share the same view.
    tb_bin = _resolve_tensorboard_binary(projects_dir, project)
    if tb_bin:
        tb_port = _find_free_port()
        if tb_port:
            try:
                tb_process = subprocess.Popen(
                    _tensorboard_launch_args(tb_bin, projects_dir, name, project, tb_port),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception as e:
                log.warning("Failed to start tensorboard for %s run %d: %s", name, run_id, e)
                tb_port = None

    with _lock:
        _running[run_id] = {
            "process": proc,
            "log_path": log_path,
            "tb_process": tb_process,
            "tb_port": tb_port,
            "started_at": time.time(),
            "run_id": run_id,
            "project_name": name,
            "branch": branch,
            "workspace_dir": workspace_dir,
        }

    _update_project_json(projects_dir, name,
                         train_status="running", train_pid=proc.pid,
                         last_run_at=time.time())

    thread = threading.Thread(
        target=_monitor_process, args=(projects_dir, name, run_id), daemon=True
    )
    thread.start()

def stop_training(projects_dir, name, run_id=None):  # NOSONAR — sequential stop/cleanup pipeline
    """Stop a training run. run_id selects which run; omit if only one is active."""
    primary_ws = os.path.join(projects_dir, name, "workspace")

    with _lock:
        if run_id is not None:
            info = _running.get(run_id)
            if not info or info.get("project_name") != name:
                return {"error": f"Run {run_id} not found for project {name}"}
        else:
            project_runs = [rid for rid, info in _running.items()
                            if info.get("project_name") == name]
            if not project_runs:
                try:
                    config_path = os.path.join(projects_dir, name, _PROJECT_FILE)  # NOSONAR
                    project = Project.load(config_path)
                    if project.train_status == "running":
                        project.train_status = "stopped"
                        project.save(projects_dir)
                        return {"stopped": True}
                except Exception:
                    pass
                return {"error": "Training is not running"}
            if len(project_runs) > 1:
                return {"error": "Multiple runs active — specify run_id"}
            run_id = project_runs[0]
            info = _running.get(run_id)
            if info is None:
                return {"error": "Training is not running"}

        proc = info["process"]
        workspace_dir = info.get("workspace_dir", primary_ws)

    if proc is not None:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
                proc.wait(timeout=3)
            except (ProcessLookupError, OSError):
                pass

        exit_code = proc.returncode if proc.returncode is not None else -15
    else:
        exit_code = -15

    with _lock:
        info = _running.pop(run_id, None)
        if info:
            tb = info.get("tb_process")
            tb_port = info.get("tb_port")
            if tb and tb.poll() is None and tb_port:
                _tb_running[name] = {
                    "tb_process": tb,
                    "tb_port": tb_port,
                    "last_access": time.time(),
                }
                log.info("Migrated TB for %s run %d to standalone", name, run_id)

    # File I/O and DB writes outside lock to avoid blocking other threads
    if info:
        log_path = info.get("log_path")
        started_at = info.get("started_at")

        if log_path and started_at:
            _append_run_footer(log_path, exit_code, started_at)

        archived_log_path = None
        if log_path and os.path.isfile(log_path):
            archived_log_path = _archive_run_log(projects_dir, name, run_id, log_path)

        if started_at:
            _finalize_run_record(run_id, exit_code, started_at, archived_log_path)

    is_parallel = workspace_dir != primary_ws
    if is_parallel:
        try:
            shutil.rmtree(workspace_dir)
            log.info("Deleted parallel workspace %s", workspace_dir)
        except Exception as e:
            log.warning("Failed to delete parallel workspace %s: %s", workspace_dir, e)

    with _lock:
        remaining = _get_runs_for_project(name)
    if not remaining:
        _update_project_json(projects_dir, name, train_status="stopped", train_pid=0)

    return {"status": "stopped", "run_id": run_id}


def get_training_status(name: str) -> dict:
    """Backward-compat: return status for the first active run, or idle."""
    runs = get_runs_for_project(name)
    if runs:
        r = runs[0]
        return {
            "status": r["status"],
            "pid": r["pid"],
            "run_id": r["run_id"],
            "started_at": None,
            "tb_port": r["tb_port"],
            "elapsed": r["elapsed"],
            "resources": r["resources"],
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
        "resources": None,
    }


def start_tensorboard(projects_dir, name):  # NOSONAR — sequential TB launch pipeline
    """Start tensorboard on-demand for a project. Returns existing port if already running."""
    # Check if TB is already running (from training or standalone)
    with _lock:
        # Check if any active run for this project already has TB
        for run_info in _running.values():
            if run_info.get("project_name") == name and run_info.get("tb_port"):
                tb = run_info.get("tb_process")
                if tb and tb.poll() is None:
                    return {"tb_port": run_info["tb_port"], "status": "already_running"}
        tb_info = _tb_running.get(name)
        if tb_info:
            tb = tb_info.get("tb_process")
            if tb and tb.poll() is None:
                tb_info["last_access"] = time.time()
                return {"tb_port": tb_info["tb_port"]}
            else:
                del _tb_running[name]

    config_path = os.path.join(projects_dir, name, _PROJECT_FILE)  # NOSONAR
    if not os.path.isfile(config_path):
        return {"error": "Project not found"}

    with open(config_path) as f:
        project = json.load(f)

    tb_bin = _resolve_tensorboard_binary(projects_dir, project)
    if not tb_bin:
        return {"error": "Tensorboard not found in project environment"}

    tb_port = _find_free_port()
    if not tb_port:
        return {"error": "No free port available for Tensorboard"}

    try:
        tb_process = subprocess.Popen(  # NOSONAR
            _tensorboard_launch_args(tb_bin, projects_dir, name, project, tb_port),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except ValueError as e:
        return {"error": str(e)}
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
