"""Folders are implicit: a folder exists while a project (or pipeline) names it."""
import os

from models.project import Project, normalize_folder

_PROJECT_FILE = "project.json"


def _project_config_paths(projects_dir):
    if not os.path.isdir(projects_dir):
        return
    for name in sorted(os.listdir(projects_dir)):
        path = os.path.join(projects_dir, name, _PROJECT_FILE)  # NOSONAR
        if os.path.isfile(path):
            yield path


def list_folders(projects_dir, pipelines_dir=None) -> list[str]:
    folders = set()
    for path in _project_config_paths(projects_dir):
        try:
            folder = Project.load(path).folder
        except Exception:
            continue
        if folder:
            folders.add(folder)
    return sorted(folders, key=str.lower)


def rename_folder(projects_dir, old, new, pipelines_dir=None) -> int:
    """Move every member of folder `old` to `new`. Returns how many items moved."""
    old = normalize_folder(old)
    new = normalize_folder(new)
    if not old:
        raise ValueError("folder to rename is required")
    moved = 0
    for path in _project_config_paths(projects_dir):
        project = Project.load(path)
        if project.folder == old:
            project.folder = new
            project.save(projects_dir)
            moved += 1
    return moved
