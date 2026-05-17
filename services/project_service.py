import os
import json as _json
import posixpath
import shutil
import subprocess
import threading
import logging
import time

from models.project import Project
from services.python_versions import find_python, _find_conda_bin

log = logging.getLogger(__name__)

CONDA_ENV_PREFIX = "beekeeper-"
RESERVED_OUTPUT_PATH_ROOTS = {".git", "persistent", "run_logs"}


def _normalize_workspace_path(path: str, label: str = "Output path") -> str:
    raw = (path or "").strip()
    if not raw:
        raise ValueError(f"{label} cannot be empty.")
    if "\\" in raw:
        raise ValueError(f"{label} '{raw}' must use forward slashes.")
    if raw.startswith("/"):
        raise ValueError(f"{label} '{raw}' must be relative to the workspace.")
    raw_parts = raw.split("/")
    if any(part in ("", ".", "..") for part in raw_parts):
        raise ValueError(f"{label} '{raw}' must be a workspace-relative directory path.")
    normalized = posixpath.normpath(raw)
    parts = normalized.split("/")
    if normalized in ("", ".") or any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"{label} '{raw}' must be a workspace-relative directory path.")
    return normalized


def _paths_overlap(left: str, right: str) -> bool:
    left_parts = left.split("/")
    right_parts = right.split("/")
    shorter, longer = (
        (left_parts, right_parts)
        if len(left_parts) <= len(right_parts)
        else (right_parts, left_parts)
    )
    return shorter == longer[:len(shorter)]


def validate_workspace_path(path: str, label: str = "Path") -> str:
    return _normalize_workspace_path(path, label)


def parse_output_paths(value) -> list[str]:
    """Parse API/form output path input into normalized workspace-relative paths."""
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = []
        for line in value.splitlines():
            raw_items.extend(line.split(","))
    elif isinstance(value, (list, tuple)):
        raw_items = []
        for item in value:
            if isinstance(item, str):
                raw_items.extend(item.splitlines())
            else:
                raw_items.append(str(item))
    else:
        raise ValueError("output_paths must be a list of strings or newline-separated text.")

    paths = []
    seen = set()
    for item in raw_items:
        item = (item or "").strip()
        if not item:
            continue
        normalized = _normalize_workspace_path(item)
        if normalized in seen:
            raise ValueError(f"Duplicate output path '{normalized}'.")
        seen.add(normalized)
        paths.append(normalized)
    return paths


def validate_output_paths(output_paths, tensorboard_log_dir="runs") -> list[str]:
    """Validate protected output paths against Beekeeper-managed workspace paths."""
    paths = parse_output_paths(output_paths)
    tb_path = _normalize_workspace_path(tensorboard_log_dir or "runs")

    for path in paths:
        root = path.split("/", 1)[0]
        if root in RESERVED_OUTPUT_PATH_ROOTS:
            raise ValueError(f"Output path '{path}' is reserved by Beekeeper.")
        if _paths_overlap(path, tb_path):
            raise ValueError(f"Output path '{path}' overlaps TensorBoard log dir '{tb_path}'.")

    for i, path in enumerate(paths):
        for other in paths[i + 1:]:
            if _paths_overlap(path, other):
                raise ValueError(f"Output paths '{path}' and '{other}' overlap.")

    return paths


def _conda_env_name(project_name):
    return f"{CONDA_ENV_PREFIX}{project_name}"


def _resolve_conda_env_path(conda_bin, env_name):
    """Get the filesystem path for a named conda environment."""
    try:
        out = subprocess.run(
            [conda_bin, "info", "--envs", "--json"],
            capture_output=True, text=True, timeout=30,
        )
        data = _json.loads(out.stdout)
        for env_path in data.get("envs", []):
            if os.path.basename(env_path) == env_name:
                return env_path
    except Exception:
        pass
    return None


def create_project(projects_dir, data):
    """Create a new project: save config, then clone/venv/install in background."""
    project = Project(
        name=data["name"],
        git_url=data["git_url"],
        branch=data.get("branch", "main"),
        python_version=data.get("python_version", "3.12"),
        train_file=data.get("train_file", "train.py"),
        tensorboard_log_dir=data.get("tensorboard_log_dir", "runs"),
        requirements_file=data.get("requirements_file", "requirements.txt"),
        env_type=data.get("env_type", "venv"),
        setup_script=data.get("setup_script", ""),
        data_dir_enabled=data.get("data_dir_enabled", False),
        data_dir_local=data.get("data_dir_local", "data"),
        data_dir_remote=data.get("data_dir_remote", ""),
        output_paths=data.get("output_paths", []),
        created_at=time.time(),
    )
    project.save(projects_dir)

    thread = threading.Thread(
        target=_setup_project, args=(projects_dir, project), daemon=True
    )
    thread.start()

    return project


def retry_setup(projects_dir, name):
    """Re-run setup for a project in error state with a clean workspace."""
    config_path = os.path.join(projects_dir, name, "project.json")
    project = Project.load(config_path)

    # Clean workspace to force fresh git clone (preserves venv and data symlinks point to external data)
    workspace_dir = os.path.join(projects_dir, name, "workspace")
    if os.path.isdir(workspace_dir):
        log.info("Retry setup: removing existing workspace for %s", name)
        shutil.rmtree(workspace_dir)

    project.setup_status = "pending"
    project.setup_error = ""
    project.save(projects_dir)

    thread = threading.Thread(
        target=_setup_project, args=(projects_dir, project, True), daemon=True
    )
    thread.start()


def _setup_project(projects_dir, project, is_retry=False):
    """Clone repo, create env, install deps. Updates project.json status as it goes."""
    log.info("Setup thread started for project: %s", project.name)

    try:
        project_dir = os.path.join(projects_dir, project.name)
        workspace_dir = os.path.join(project_dir, "workspace")

        def _save_status(status, error=None):
            project.setup_status = status
            project.setup_error = error
            project.save(projects_dir)

        # --- Git clone (skip if workspace already exists from a previous attempt) ---
        if os.path.isdir(workspace_dir):
            log.info("Retry: workspace already exists for %s, skipping clone", project.name)
        else:
            _save_status("cloning")
            try:
                subprocess.run(
                    ["git", "clone", "-b", project.branch, project.git_url, workspace_dir],
                    check=True, capture_output=True, text=True, timeout=300,
                )
            except subprocess.CalledProcessError as e:
                _save_status("error", f"Git clone failed: {e.stderr.strip()}")
                return
            except subprocess.TimeoutExpired:
                _save_status("error", "Git clone timed out (5 min)")
                return

        # --- Create environment (skip if already exists) ---
        if project.env_type == "conda":
            env_name = _conda_env_name(project.name)
            conda_bin = _find_conda_bin()
            env_path = _resolve_conda_env_path(conda_bin, env_name) if conda_bin else None
            if env_path:
                log.info("Retry: conda env already exists for %s, skipping", project.name)
                pip_bin = os.path.join(env_path, "bin", "pip")
            else:
                _save_status("creating_env")
                pip_bin = _create_conda_env(project, _save_status)
                env_path = _resolve_conda_env_path(conda_bin, env_name) if conda_bin else None
            # Store for setup script execution
            use_conda = True
        else:
            env_dir = os.path.join(project_dir, "venv")
            if os.path.isdir(env_dir):
                log.info("Retry: venv already exists for %s, skipping", project.name)
                pip_bin = os.path.join(env_dir, "bin", "pip")
            else:
                _save_status("creating_env")
                pip_bin = _create_venv(project, env_dir, _save_status)
            # Store for setup script execution
            use_conda = False
            env_path = env_dir

        if pip_bin is None:
            return  # _save_status("error", ...) already called

        # --- Data dir symlink (before setup script so setup.sh can use it) ---
        if project.data_dir_enabled and project.data_dir_remote:
            local_path = os.path.join(workspace_dir, project.data_dir_local)
            if os.path.islink(local_path):
                if os.readlink(local_path) != project.data_dir_remote:
                    os.unlink(local_path)
                    os.symlink(project.data_dir_remote, local_path)
            elif os.path.exists(local_path):
                _save_status(
                    "error",
                    f"'{project.data_dir_local}' already exists in the repository and is not a symlink. "
                    f"Remove it from the repo, then re-setup the project.",
                )
                return
            elif os.path.isdir(project.data_dir_remote):
                os.symlink(project.data_dir_remote, local_path)
            else:
                _save_status("error", f"Data directory '{project.data_dir_remote}' does not exist on this server.")
                return

        # --- Setup script ---
        if project.setup_script:
            script_path = os.path.join(workspace_dir, project.setup_script)
            if os.path.isfile(script_path):
                _save_status("running_setup_script")
                try:
                    # Run setup script in the activated environment
                    if use_conda:
                        # Use conda run to execute in the conda environment
                        cmd = [conda_bin, "run", "-n", env_name, "bash", script_path]
                    else:
                        # For venv, set environment variables so python/pip resolve correctly
                        cmd = ["bash", script_path]
                        env = os.environ.copy()
                        env["VIRTUAL_ENV"] = env_path
                        env["PATH"] = f"{env_path}/bin:{env.get('PATH', '')}"

                    subprocess.run(
                        cmd,
                        cwd=workspace_dir,
                        env=env if not use_conda else None,
                        check=True, capture_output=True, text=True, timeout=300,
                    )
                except subprocess.CalledProcessError as e:
                    _save_status("error", f"Setup script failed: {e.stderr.strip()[-500:]}")
                    return
                except subprocess.TimeoutExpired:
                    _save_status("error", "Setup script timed out (5 min)")
                    return

        # --- Pip install ---
        req_path = os.path.join(workspace_dir, project.requirements_file)
        if os.path.isfile(req_path):
            _save_status("installing_deps")
            try:
                subprocess.run(
                    [pip_bin, "install", "-r", req_path],
                    check=True, capture_output=True, text=True, timeout=600,
                )
            except subprocess.CalledProcessError as e:
                _save_status("error", f"Pip install failed: {e.stderr.strip()[-500:]}")
                return

        _save_status("ready")
        log.info("Setup completed successfully for project: %s", project.name)

    except Exception as e:
        log.exception("Setup thread crashed for %s", project.name)
        try:
            project.setup_status = "error"
            project.setup_error = f"Setup crashed: {str(e)[:500]}"
            project.save(projects_dir)
        except Exception:
            log.exception("Failed to save error status for %s", project.name)


def _create_venv(project, env_dir, _save_status):
    """Create a standard Python venv. Returns pip path or None on failure."""
    python_bin = find_python(project.python_version)
    if not python_bin:
        _save_status("error", f"No Python found for version {project.python_version}")
        return None
    try:
        subprocess.run(
            [python_bin, "-m", "venv", env_dir],
            check=True, capture_output=True, text=True, timeout=120,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        _save_status("error", f"Venv creation failed: {e}")
        return None
    return os.path.join(env_dir, "bin", "pip")


def _create_conda_env(project, _save_status):
    """Create a named conda environment. Returns pip path or None on failure."""
    conda_bin = _find_conda_bin()
    if not conda_bin:
        _save_status("error", "conda not found on this system")
        return None

    env_name = _conda_env_name(project.name)
    try:
        subprocess.run(
            [
                conda_bin, "create", "-y", "-n", env_name,
                f"python={project.python_version}", "pip",
            ],
            check=True, capture_output=True, text=True, timeout=300,
        )
    except subprocess.CalledProcessError as e:
        _save_status("error", f"Conda env creation failed: {e.stderr.strip()[-500:]}")
        return None

    env_path = _resolve_conda_env_path(conda_bin, env_name)
    if not env_path:
        _save_status("error", f"Conda env created but could not resolve path for '{env_name}'")
        return None

    return os.path.join(env_path, "bin", "pip")


def delete_project(projects_dir, name):
    """Remove a project directory and its conda env (if any)."""
    project_dir = os.path.join(projects_dir, name)
    config_path = os.path.join(project_dir, "project.json")  # NOSONAR

    # Clean up conda env if this was a conda project
    if os.path.isfile(config_path):
        try:
            with open(config_path) as f:
                data = _json.load(f)
            if data.get("env_type") == "conda":
                conda_bin = _find_conda_bin()
                if conda_bin:
                    env_name = _conda_env_name(name)
                    subprocess.run(
                        [conda_bin, "env", "remove", "-y", "-n", env_name],
                        capture_output=True, text=True, timeout=120,
                    )
        except Exception:
            pass

    if os.path.isdir(project_dir):
        shutil.rmtree(project_dir)
