"""Pipeline checks that depend on project settings.

pipeline_schema.validate() is pure; these read project.json, so they run when a
pipeline is saved or validated and again when each step launches (settings can
change in between). A missing project is skipped here: it is already a start-time
error. Errors use the schema's {path, message} form, plus error_code and fix when
Beekeeper can offer a fix. The fix is never applied here.
"""
import json
import os

from services.pipeline_schema import normalize_rel_path

INHERIT_PATH_NOT_DECLARED = "INHERIT_PATH_NOT_DECLARED"
TASK_NEEDS_LOCAL_ENV = "TASK_NEEDS_LOCAL_ENV"


def load_project(projects_dir, name) -> dict | None:
    try:
        with open(os.path.join(projects_dir, name, "project.json")) as f:  # NOSONAR - name validated by the schema
            return json.load(f)
    except (OSError, ValueError):
        return None


def _has_local_env(projects_dir, project) -> bool:
    from services.project_service import local_env_missing
    return not local_env_missing(projects_dir, project)


def _output_paths(project) -> set[str]:
    return {p for p in (normalize_rel_path(p) for p in project.get("output_paths") or []) if p}


def _step_paths(step, project) -> set[str] | None:
    """The output paths a step has, or None when it can't be known (its project is missing)."""
    if step.get("project"):
        return _output_paths(project) if project else None
    return set(step.get("inherit") or [])


def _reserved_link(project, rel) -> str | None:
    name = project["name"]
    if project.get("data_dir_enabled") and normalize_rel_path(project.get("data_dir_local") or "data") == rel:
        return f"'{rel}' is {name}'s data dir link"
    if normalize_rel_path(project.get("tensorboard_log_dir") or "runs") == rel:
        return f"'{rel}' is {name}'s TensorBoard dir"
    return None


def _check_local_env(projects_dir, index, step, project, errors):
    from services import remote_runs
    if step["type"] != "task" or not project or not remote_runs.is_remote(project):
        return
    if _has_local_env(projects_dir, project):
        return
    name = step["project"]
    errors.append({
        "path": f"steps[{index}].project",
        "message": f"{name} runs on Runpod and has no local environment for tasks",
        "error_code": TASK_NEEDS_LOCAL_ENV,
        "fix": {"call": "build_local_env", "project": name},
    })


def _check_inherit(index, step, project, prev, prev_project, errors):
    path = f"steps[{index}].inherit"
    prev_paths = _step_paths(prev, prev_project) if prev.get("project") else None
    for rel in step.get("inherit") or []:
        if prev_paths is not None and rel not in prev_paths:
            errors.append({"path": path,
                           "message": f"'{rel}' isn't an output path of the previous step ({prev['name']})"})
        if not step.get("project") or not project:
            continue   # a plain task has every path it inherits
        reserved = _reserved_link(project, rel)
        if reserved:
            errors.append({"path": path, "message": reserved})
        elif rel not in _output_paths(project):
            errors.append({
                "path": path,
                "message": f"{step['project']} doesn't list '{rel}' as an output path",
                "error_code": INHERIT_PATH_NOT_DECLARED,
                "fix": {"call": "update_project", "project": step["project"], "output_paths_add": [rel]},
            })


def check_steps(projects_dir, steps, indices=None) -> list[dict]:
    """Errors for the given step indexes (all steps by default) of a validated definition."""
    cache = {}

    def project_of(step):
        name = step.get("project")
        if not name:
            return None
        if name not in cache:
            cache[name] = load_project(projects_dir, name)
        return cache[name]

    errors = []
    for index in (range(len(steps)) if indices is None else indices):
        step = steps[index]
        project = project_of(step)
        _check_local_env(projects_dir, index, step, project, errors)
        if index > 0 and step.get("inherit"):
            prev = steps[index - 1]
            _check_inherit(index, step, project, prev, project_of(prev), errors)
    return errors
