import logging
import os
import shutil

log = logging.getLogger(__name__)

# Written into each persistent run folder; names the training_runs.id (run_pk) that owns it.
MARKER = ".beekeeper_run_pk"


def read_run_marker(path: str) -> int | None:
    """The run_pk named by the ownership marker in a run folder, or None if absent/unreadable."""
    try:
        with open(os.path.join(path, MARKER)) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _owned_by_other_run(path: str, run: dict) -> bool:
    owner = read_run_marker(path)
    if owner is not None and owner != run.get("id"):
        log.warning("Not deleting %s: it belongs to run_pk %s, not run_pk %s", path, owner, run.get("id"))
        return True
    return False


def persistent_runs_root(projects_dir: str, project_name: str) -> str:
    return os.path.join(projects_dir, project_name, "persistent", "runs")


def delete_path(path: str, label: str):
    try:
        if os.path.islink(path) or os.path.isfile(path):  # NOSONAR
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
        persistent = os.path.join(projects_dir, project_name, run["persistent_dir"])
        if not _owned_by_other_run(persistent, run):
            delete_path(persistent, "persistent run directory")
    elif run.get("tensorboard_dir"):
        project_relative = os.path.join(projects_dir, project_name, run["tensorboard_dir"])
        workspace_relative = os.path.join(projects_dir, project_name, "workspace", run["tensorboard_dir"])
        target = project_relative if os.path.lexists(project_relative) else workspace_relative
        if not _owned_by_other_run(target, run):
            delete_path(target, "TensorBoard logs")


def clear_persistent_runs(projects_dir: str, project_name: str):
    runs_root = persistent_runs_root(projects_dir, project_name)
    if os.path.isdir(runs_root):
        shutil.rmtree(runs_root, ignore_errors=True)
    os.makedirs(runs_root, exist_ok=True)  # NOSONAR
