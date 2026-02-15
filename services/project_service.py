import os
import shutil
import subprocess
import threading
import logging

from models.project import Project

log = logging.getLogger(__name__)


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
    )
    project.save(projects_dir)

    # Run the slow setup steps in a background thread
    thread = threading.Thread(
        target=_setup_project, args=(projects_dir, project), daemon=True
    )
    thread.start()

    return project


def _setup_project(projects_dir, project):
    """Clone repo, create venv, install deps. Updates project.json status as it goes."""
    project_dir = os.path.join(projects_dir, project.name)
    src_dir = os.path.join(project_dir, "src")
    venv_dir = os.path.join(project_dir, "venv")

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

    # --- Create venv ---
    _save_status("creating_venv")
    python_bin = f"python{project.python_version}"
    try:
        subprocess.run(
            [python_bin, "-m", "venv", venv_dir],
            check=True, capture_output=True, text=True, timeout=120,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        _save_status("error", f"Venv creation failed: {e}")
        return

    # --- Pip install ---
    req_path = os.path.join(src_dir, project.requirements_file)
    if os.path.isfile(req_path):
        _save_status("installing_deps")
        pip_bin = os.path.join(venv_dir, "bin", "pip")
        try:
            subprocess.run(
                [pip_bin, "install", "-r", req_path],
                check=True, capture_output=True, text=True, timeout=600,
            )
        except subprocess.CalledProcessError as e:
            _save_status("error", f"Pip install failed: {e.stderr.strip()[-500:]}")
            return

    _save_status("ready")


def delete_project(projects_dir, name):
    """Remove a project directory entirely."""
    project_dir = os.path.join(projects_dir, name)
    if os.path.isdir(project_dir):
        shutil.rmtree(project_dir)
