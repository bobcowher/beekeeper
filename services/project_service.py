import os
import json as _json
import shutil
import subprocess
import threading
import logging

from models.project import Project
from services.python_versions import find_python, _find_conda_bin

log = logging.getLogger(__name__)

CONDA_ENV_PREFIX = "beekeeper-"


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
    )
    project.save(projects_dir)

    thread = threading.Thread(
        target=_setup_project, args=(projects_dir, project), daemon=True
    )
    thread.start()

    return project


def _setup_project(projects_dir, project):
    """Clone repo, create env, install deps. Updates project.json status as it goes."""
    project_dir = os.path.join(projects_dir, project.name)
    src_dir = os.path.join(project_dir, "src")

    def _save_status(status, error=None):
        project.setup_status = status
        project.setup_error = error
        project.save(projects_dir)

    # --- Git clone ---
    _save_status("cloning")
    try:
        subprocess.run(
            ["git", "clone", "-b", project.branch, project.git_url, src_dir],
            check=True, capture_output=True, text=True, timeout=300,
        )
    except subprocess.CalledProcessError as e:
        _save_status("error", f"Git clone failed: {e.stderr.strip()}")
        return
    except subprocess.TimeoutExpired:
        _save_status("error", "Git clone timed out (5 min)")
        return

    # --- Create environment ---
    _save_status("creating_env")

    if project.env_type == "conda":
        pip_bin = _create_conda_env(project, _save_status)
    else:
        env_dir = os.path.join(project_dir, "venv")
        pip_bin = _create_venv(project, env_dir, _save_status)

    if pip_bin is None:
        return  # _save_status("error", ...) already called

    # --- Pip install ---
    req_path = os.path.join(src_dir, project.requirements_file)
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
    config_path = os.path.join(project_dir, "project.json")

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
