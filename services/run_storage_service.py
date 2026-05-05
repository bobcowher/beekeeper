import logging
import os
import shutil

log = logging.getLogger(__name__)


def persistent_runs_root(projects_dir: str, project_name: str) -> str:
    return os.path.join(projects_dir, project_name, "persistent", "runs")


def delete_path(path: str, label: str):
    try:
        if os.path.islink(path) or os.path.isfile(path):
            os.unlink(path)
            log.info("Deleted %s: %s", label, path)
        elif os.path.isdir(path):
            shutil.rmtree(path)
            log.info("Deleted %s: %s", label, path)
    except Exception as e:
        log.warning("Failed to delete %s %s: %s", label, path, e)


def delete_run_storage(projects_dir: str, project_name: str, run: dict):
    if run.get("log_file_path"):
        delete_path(
            os.path.join(projects_dir, project_name, run["log_file_path"]),
            "archived log",
        )

    if run.get("persistent_dir"):
        delete_path(
            os.path.join(projects_dir, project_name, run["persistent_dir"]),
            "persistent run directory",
        )
    elif run.get("tensorboard_dir"):
        project_relative = os.path.join(projects_dir, project_name, run["tensorboard_dir"])
        workspace_relative = os.path.join(projects_dir, project_name, "workspace", run["tensorboard_dir"])
        if os.path.lexists(project_relative):
            delete_path(project_relative, "TensorBoard logs")
        else:
            delete_path(workspace_relative, "TensorBoard logs")


def clear_persistent_runs(projects_dir: str, project_name: str):
    runs_root = persistent_runs_root(projects_dir, project_name)
    if os.path.isdir(runs_root):
        shutil.rmtree(runs_root, ignore_errors=True)
    os.makedirs(runs_root, exist_ok=True)
