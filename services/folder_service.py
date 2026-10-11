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


def _pipelines(pipelines_dir):
    if not pipelines_dir:
        return []
    from services.pipeline_store import list_pipelines
    return [p for p in list_pipelines(pipelines_dir) if not p.get("error")]


def list_folders(projects_dir, pipelines_dir=None) -> list[str]:
    folders = set()
    for path in _project_config_paths(projects_dir):
        try:
            folder = Project.load(path).folder
        except Exception:
            continue
        if folder:
            folders.add(folder)
    folders.update(p["folder"] for p in _pipelines(pipelines_dir) if p["folder"])
    return sorted(folders, key=str.lower)


def rename_folder(projects_dir, old, new, pipelines_dir=None) -> int:
    """Move every member of folder `old` to `new`. Returns how many items moved."""
    old = normalize_folder(old)
    new = normalize_folder(new)
    if not old:
        raise ValueError("folder to rename is required")
    projects = [Project.load(path) for path in _project_config_paths(projects_dir)]
    pipelines = _pipelines(pipelines_dir)
    moved = 0
    for project in projects:
        if project.folder == old:
            project.folder = new
            project.save(projects_dir)
            moved += 1
    from services.pipeline_store import save_pipeline
    for defn in pipelines:
        if defn["folder"] == old:
            defn["folder"] = new
            save_pipeline(pipelines_dir, defn)
            moved += 1
    return moved
