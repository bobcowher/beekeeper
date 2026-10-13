"""
REST API v1 for Beekeeper.

All endpoints return JSON with a consistent envelope:
    {"success": true, "data": {...}}
    {"success": false, "error": {"code": "...", "message": "..."}}
"""

import os
import re
import subprocess
import time
from typing import NoReturn
from flask import Blueprint, abort, current_app, jsonify, request, Response

from models.project import Project, SETUP_ACTIVE_STATUSES, normalize_folder
from services.folder_service import list_folders, rename_folder
from services.process_manager import can_start_training, start_training, stop_training, get_training_status, get_runs_for_project, get_run_log_path
from services.stats_service import get_all_stats, get_cpu_stats, get_memory_stats, get_gpu_stats, get_gpu_platform_info
from services.db_service import get_db
from services.auth_service import api_key_required
from services.project_service import ensure_data_dir_symlink, validate_output_paths
from services.git_utils import git_env
from services.run_storage_service import delete_run_storage

# Reuse helpers from existing routes
from routes.training import _tail_offset
from routes.files import _safe_path, _fmt_size, _zip_directory
from routes.runs import _resolve_run, _safe_run_path
from services.run_refs import public_run, run_ref
from services import pipeline_engine, pipeline_store
from services.pipeline_schema import (NAME_RE as PIPELINE_NAME_RE, PipelineValidationError,
                                      draft_yaml, parse_text, to_yaml, valid_branch_name,
                                      validate as validate_pipeline)

api_v1_bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")

SERVER_VERSION = "1.1.0"
MIN_MCP_VERSION = "0.5.0"
PROJECT_FILE = "project.json"
MCP_SERVER_FILE = "mcp_server.py"
PROJECT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
EPISODE_PATTERN = re.compile(
    r'Episode\s+(\d+)\s*\|\s*reward:\s*([-\d.]+)'
    r'(?:\s*\|\s*epsilon:\s*([\d.]+))?'
    r'(?:\s*\|\s*steps:\s*(\d+))?',
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def api_response(data=None, error_code=None, error_message=None, status_code=200):
    """Build a consistent API response."""
    if error_code:
        return jsonify({
            "success": False,
            "error": {"code": error_code, "message": error_message}
        }), status_code
    return jsonify({"success": True, "data": data}), status_code


def _abort_json(status_code: int, error_code: str, error_message: str) -> NoReturn:
    """Abort the request with a JSON error response."""
    resp = jsonify({"success": False, "error": {"code": error_code, "message": error_message}})
    resp.status_code = status_code
    abort(resp)


def load_project(name) -> Project:
    """Load a project by name. Aborts with JSON 404/500 if not found or unreadable."""
    projects_dir = current_app.config["PROJECTS_DIR"]
    config_path = os.path.join(projects_dir, name, PROJECT_FILE)
    if not os.path.isfile(config_path):
        _abort_json(404, "NOT_FOUND", f"Project '{name}' not found")
    try:
        return Project.load(config_path)
    except Exception as e:
        _abort_json(500, "LOAD_ERROR", f"Failed to load project: {e}")


def _project_creation_fields(data, projects_dir):
    name = data.get("name", "").strip()
    if not name:
        return None, api_response(error_code="MISSING_NAME", error_message="Project name is required", status_code=400)
    if not PROJECT_NAME_RE.match(name):
        return None, api_response(
            error_code="INVALID_NAME",
            error_message="Invalid project name. Use only letters, numbers, hyphens, underscores.",
            status_code=400,
        )
    git_url = data.get("git_url", "").strip()
    if not git_url:
        return None, api_response(error_code="MISSING_GIT_URL", error_message="Git URL is required", status_code=400)
    try:
        folder = normalize_folder(data.get("folder", ""))
    except ValueError as e:
        return None, api_response(error_code="INVALID_FOLDER", error_message=str(e), status_code=400)
    if os.path.exists(os.path.join(projects_dir, name)):
        return None, api_response(
            error_code="DUPLICATE_NAME", error_message=f"Project '{name}' already exists", status_code=409)
    return {"name": name, "git_url": git_url, "folder": folder}, None


def _validate_data_directory(data):
    enabled = data.get("data_dir_enabled", False)
    remote = data.get("data_dir_remote", "").strip()
    if enabled and not remote:
        return api_response(
            error_code="MISSING_DATA_DIR",
            error_message="System data path is required when data directory is enabled",
            status_code=400,
        )
    if enabled and not os.path.isdir(remote):
        return api_response(
            error_code="INVALID_DATA_DIR",
            error_message=f"System data path '{remote}' does not exist or is not a directory",
            status_code=400,
        )
    return None


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@api_v1_bp.route("/projects", methods=["GET"])
@api_key_required
def list_projects():
    """List all projects with basic info and training status."""
    projects_dir = current_app.config["PROJECTS_DIR"]
    projects = []
    folder_filter = request.args.get("folder")

    if not os.path.isdir(projects_dir):
        return api_response(data={"projects": []})

    for name in sorted(os.listdir(projects_dir)):
        config_path = os.path.join(projects_dir, name, PROJECT_FILE)
        if os.path.isfile(config_path):
            try:
                project = Project.load(config_path)
                if folder_filter is not None and project.folder != folder_filter.strip():
                    continue
                status = get_training_status(name)
                projects.append({
                    "name": project.name,
                    "git_url": project.git_url,
                    "branch": project.branch,
                    "setup_status": project.setup_status,
                    "train_status": status["status"],
                    "pinned": project.pinned,
                    "last_run_at": project.last_run_at,
                    "folder": project.folder,
                })
            except Exception:
                continue

    return api_response(data={"projects": projects})


@api_v1_bp.route("/projects", methods=["POST"])
@api_key_required
def create_project():
    """Create a new project from a git repository."""
    from services.project_service import create_project as create_project_service

    data = request.get_json() or {}
    projects_dir = current_app.config["PROJECTS_DIR"]
    fields, error = _project_creation_fields(data, projects_dir)
    if error is not None:
        return error
    name, git_url, folder = fields["name"], fields["git_url"], fields["folder"]
    data_dir_enabled = data.get("data_dir_enabled", False)
    error = _validate_data_directory(data)
    if error is not None:
        return error

    tensorboard_log_dir = data.get("tensorboard_log_dir", "runs").strip() or "runs"
    try:
        output_paths = validate_output_paths(data.get("output_paths", []), tensorboard_log_dir)
    except ValueError as e:
        return api_response(
            error_code="INVALID_OUTPUT_PATHS",
            error_message=str(e),
            status_code=400,
        )

    # Build project data with defaults
    project_data = {
        "name": name,
        "git_url": git_url,
        "branch": data.get("branch", "main").strip() or "main",
        "python_version": data.get("python_version", "3.12"),
        "train_file": data.get("train_file", "train.py").strip() or "train.py",
        "tensorboard_log_dir": tensorboard_log_dir,
        "requirements_file": data.get("requirements_file", "requirements.txt").strip() or "requirements.txt",
        "env_type": data.get("env_type", "venv"),
        "setup_script": data.get("setup_script", "").strip(),
        "data_dir_enabled": data_dir_enabled,
        "data_dir_local": data.get("data_dir_local", "data").strip() or "data",
        "data_dir_remote": data.get("data_dir_remote", "").strip(),
        "output_paths": output_paths,
        "folder": folder,
    }

    # Create the project
    try:
        create_project_service(projects_dir, project_data)
    except Exception as e:
        return api_response(
            error_code="CREATE_FAILED",
            error_message=f"Failed to create project: {e}",
            status_code=500
        )

    # Load and return the created project
    config_path = os.path.join(projects_dir, name, PROJECT_FILE)
    project = Project.load(config_path)

    return api_response(
        data={"project": project.to_dict()},
        status_code=201
    )


@api_v1_bp.route("/projects/<name>", methods=["GET"])
@api_key_required
def get_project(name):
    """Get detailed project info including training status."""
    project = load_project(name)
    status = get_training_status(name)
    total_runtime_seconds = get_db().get_project_total_runtime(name)

    return api_response(data={
        "project": {
            **project.to_dict(),
            "training": status,
            "total_runtime_seconds": total_runtime_seconds,
        }
    })


@api_v1_bp.route("/projects/<name>/clone", methods=["POST"])
@api_key_required
def clone_project(name):
    """Clone an existing project with a new name."""
    from services.project_service import create_project as create_project_service

    # Load the source project
    project = load_project(name)

    # Get the new name from request
    data = request.get_json() or {}
    new_name = data.get("name", "").strip()

    if not new_name:
        return api_response(
            error_code="MISSING_NAME",
            error_message="New project name is required",
            status_code=400
        )

    if not PROJECT_NAME_RE.match(new_name):
        return api_response(
            error_code="INVALID_NAME",
            error_message="Invalid project name. Use only letters, numbers, hyphens, underscores.",
            status_code=400
        )

    projects_dir = current_app.config["PROJECTS_DIR"]

    # Check for duplicate
    if os.path.exists(os.path.join(projects_dir, new_name)):
        return api_response(
            error_code="DUPLICATE_NAME",
            error_message=f"Project '{new_name}' already exists",
            status_code=409
        )

    # Copy settings from source project, with optional overrides
    project_data = project.to_dict()
    project_data["name"] = new_name

    # Allow overriding branch
    if "branch" in data:
        project_data["branch"] = data["branch"].strip() or project.branch

    # Reset status fields for the new clone
    project_data["setup_status"] = "pending"
    project_data["setup_error"] = ""
    project_data["train_status"] = "idle"
    project_data["train_pid"] = 0
    project_data["pinned"] = False
    project_data["last_run_at"] = 0.0

    # Create the cloned project
    try:
        create_project_service(projects_dir, project_data)
    except Exception as e:
        return api_response(
            error_code="CLONE_FAILED",
            error_message=f"Failed to clone project: {e}",
            status_code=500
        )

    # Load and return the cloned project
    config_path = os.path.join(projects_dir, new_name, PROJECT_FILE)
    cloned_project = Project.load(config_path)

    return api_response(
        data={
            "project": cloned_project.to_dict(),
            "source": name
        },
        status_code=201
    )


@api_v1_bp.route("/projects/<name>/setup/retry", methods=["POST"])
@api_key_required
def retry_project_setup(name):
    """
    Retry setup for a project that failed.

    Safe to call when setup_status is 'error'. Skips steps already completed
    (clone if workspace/ exists, env creation if venv/conda env exists).
    Runs asynchronously — poll GET /projects/<name> for setup_status.
    """
    project = load_project(name)

    if project.setup_status == "ready":
        return api_response(
            error_code="ALREADY_READY",
            error_message="Project setup is already complete",
            status_code=409
        )

    from services.project_service import retry_setup
    projects_dir = current_app.config["PROJECTS_DIR"]
    retry_setup(projects_dir, name)

    return api_response(data={"status": "retrying"}, status_code=202)


@api_v1_bp.route("/projects/<name>", methods=["DELETE"])
@api_key_required
def delete_project_api(name):
    """
    Delete a project and all its data.

    Stops TensorBoard if running. Cannot delete while a run is starting or
    running — stop it first.
    """
    load_project(name)

    from services.process_manager import stop_tensorboard
    from services.project_service import ProjectBusyError, delete_project
    projects_dir = current_app.config["PROJECTS_DIR"]
    try:
        delete_project(projects_dir, name)
    except ProjectBusyError as e:
        return api_response(error_code="PROJECT_BUSY", error_message=str(e), status_code=409)
    stop_tensorboard(name)

    return api_response(data={"deleted": name})


@api_v1_bp.route("/projects/<name>/rename", methods=["POST"])
@api_key_required
def rename_project_api(name):
    """
    Rename a project directory and update all run history records.

    Body: {"new_name": "my-new-name"}
    Cannot rename while setup or training is active.
    """
    import json

    load_project(name)

    status = get_training_status(name)
    if status["status"] != "idle":
        return api_response(
            error_code="TRAINING_ACTIVE",
            error_message="Cannot rename while training is active — stop training first",
            status_code=409
        )

    projects_dir = current_app.config["PROJECTS_DIR"]
    config_path = os.path.join(projects_dir, name, PROJECT_FILE)
    with open(config_path) as f:
        project_data = json.load(f)

    if project_data.get("setup_status") in SETUP_ACTIVE_STATUSES:
        return api_response(
            error_code="SETUP_ACTIVE",
            error_message="Cannot rename while setup is in progress",
            status_code=409
        )

    body = request.get_json() or {}
    new_name = body.get("new_name", "").strip()
    if not new_name or not PROJECT_NAME_RE.match(new_name):
        return api_response(
            error_code="INVALID_NAME",
            error_message="Invalid project name — use only letters, numbers, hyphens, underscores",
            status_code=400
        )

    if new_name == name:
        return api_response(data={"name": name, "renamed": False})

    new_dir = os.path.join(projects_dir, new_name)
    if os.path.exists(new_dir):
        return api_response(
            error_code="NAME_CONFLICT",
            error_message=f"A project named '{new_name}' already exists",
            status_code=409
        )

    from services.project_service import rename_project
    updated = rename_project(projects_dir, name, new_name, current_app.config.get("PIPELINES_DIR"))

    return api_response(data={
        "old_name": name,
        "name": new_name,
        "renamed": True,
        "setup_status": updated["setup_status"],
    })


def _apply_text_project_updates(project, data):
    if "folder" in data:
        try:
            project.folder = normalize_folder(data["folder"])
        except ValueError as e:
            return api_response(error_code="INVALID_FOLDER", error_message=str(e), status_code=400)
    for field in ("branch", "train_file", "tensorboard_log_dir", "requirements_file"):
        if field in data:
            setattr(project, field, data[field].strip() or getattr(project, field))
    if "setup_script" in data:
        project.setup_script = data["setup_script"].strip()
    if "env_vars" in data:
        if not isinstance(data["env_vars"], dict):
            return api_response(
                error_code="INVALID_ENV_VARS",
                error_message="env_vars must be a JSON object",
                status_code=400
            )
        project.env_vars = data["env_vars"]
    return None


def _set_integer_project_setting(project, data, field, minimum, error_code, error_message):
    if field not in data:
        return None
    try:
        setattr(project, field, max(minimum, int(data[field])))
    except (ValueError, TypeError):
        return api_response(error_code=error_code, error_message=error_message, status_code=400)
    return None


def _apply_numeric_project_updates(project, data):
    settings = [
        ("tb_logs_max_runs", 1, "INVALID_TB_LOGS_MAX_RUNS", "tb_logs_max_runs must be an integer"),
        ("run_history_max_runs", 1, "INVALID_RUN_HISTORY_MAX_RUNS", "run_history_max_runs must be an integer"),
        ("gpu_memory_minimum", 0, "INVALID_GPU_MEMORY_MINIMUM",
         "gpu_memory_minimum must be a non-negative integer (MB)"),
        ("gpu_memory_preferred", 0, "INVALID_GPU_MEMORY_PREFERRED",
         "gpu_memory_preferred must be a non-negative integer (MB)"),
    ]
    for field, minimum, code, message in settings:
        error = _set_integer_project_setting(project, data, field, minimum, code, message)
        if error is not None:
            return error
    if "gpu_enabled" in data:
        project.gpu_enabled = bool(data["gpu_enabled"])
    return None


def _apply_data_directory_update(project, data, projects_dir, name):
    data_dir_fields = {"data_dir_enabled", "data_dir_local", "data_dir_remote"}
    if not data_dir_fields.intersection(data):
        return None
    enabled = data.get("data_dir_enabled", project.data_dir_enabled)
    local = (data.get("data_dir_local", project.data_dir_local) or "").strip() or "data"
    remote = (data.get("data_dir_remote", project.data_dir_remote) or "").strip()
    if enabled and not remote:
        return api_response(
            error_code="MISSING_DATA_DIR",
            error_message="data_dir_remote is required when data_dir_enabled is true",
            status_code=400,
        )
    if enabled and not os.path.isdir(remote):
        return api_response(
            error_code="INVALID_DATA_DIR",
            error_message=f"System data path '{remote}' does not exist or is not a directory",
            status_code=400,
        )
    workspace_dir = os.path.join(projects_dir, name, "workspace")
    if enabled and os.path.isdir(workspace_dir):
        error = ensure_data_dir_symlink(workspace_dir, local, remote)
        if error:
            return api_response(error_code="DATA_DIR_CONFLICT", error_message=error, status_code=409)
    project.data_dir_enabled = bool(enabled)
    project.data_dir_local = local
    project.data_dir_remote = remote
    return None


@api_v1_bp.route("/projects/<name>", methods=["PATCH"])
@api_key_required
def update_project_api(name):
    """Update editable project settings included in the request."""
    project = load_project(name)
    data = request.get_json() or {}
    if not data:
        return api_response(error_code="NO_FIELDS", error_message="No fields to update", status_code=400)
    if get_training_status(name)["status"] == "running" and not set(data) <= {"folder"}:
        return api_response(
            error_code="TRAINING_RUNNING",
            error_message="Cannot update project while training is running — stop training first",
            status_code=409,
        )
    for updater in (_apply_text_project_updates, _apply_numeric_project_updates):
        error = updater(project, data)
        if error is not None:
            return error
    projects_dir = current_app.config["PROJECTS_DIR"]
    error = _apply_data_directory_update(project, data, projects_dir, name)
    if error is not None:
        return error
    project.save(projects_dir)
    return api_response(data={"project": project.to_dict()})


@api_v1_bp.route("/folders", methods=["GET"])
@api_key_required
def list_folders_api():
    """All folder names in use, alphabetical."""
    projects_dir = current_app.config["PROJECTS_DIR"]
    pipelines_dir = current_app.config.get("PIPELINES_DIR")
    return api_response(data={"folders": list_folders(projects_dir, pipelines_dir)})


@api_v1_bp.route("/folders/<folder>/rename", methods=["POST"])
@api_key_required
def rename_folder_api(folder):
    """Rename a folder by moving every member to new_name."""
    data = request.get_json() or {}
    try:
        moved = rename_folder(current_app.config["PROJECTS_DIR"], folder,
                              data.get("new_name", ""), current_app.config.get("PIPELINES_DIR"))
    except ValueError as e:
        return api_response(error_code="INVALID_FOLDER", error_message=str(e), status_code=400)
    return api_response(data={"renamed": moved})


# ---------------------------------------------------------------------------
# Pipelines
# ---------------------------------------------------------------------------

def _pipelines_dir():
    return current_app.config["PIPELINES_DIR"]


def _pipeline_error(e: pipeline_engine.PipelineError):
    return api_response(error_code=e.code, error_message=e.message, status_code=e.status)


def _invalid_pipeline(e: PipelineValidationError):
    resp, status = api_response(error_code="INVALID_PIPELINE", error_message=str(e), status_code=400)
    payload = resp.get_json()
    payload["error"]["details"] = {"errors": e.errors}
    return jsonify(payload), status


def _require_pipeline(name):
    # pipeline_exists rejects names that fail the name regex (fullmatch)
    if not pipeline_store.pipeline_exists(_pipelines_dir(), name):
        _abort_json(404, "NOT_FOUND", f"Pipeline '{name}' not found")


def _parse_body() -> tuple[dict, str | None]:
    """(parsed definition, raw YAML text or None for JSON bodies)."""
    if request.is_json:
        return request.get_json(silent=True), None
    text = request.get_data(as_text=True)
    return parse_text(text), text


def _save_body(name: str | None):
    data, text = _parse_body()
    if text is not None:
        target = name or (data.get("name") if isinstance(data, dict) else None)
        if not isinstance(target, str) or not PIPELINE_NAME_RE.fullmatch(target):
            validate_pipeline(data)            # raises with the precise name error
        return pipeline_store.save_pipeline_text(_pipelines_dir(), target, text)
    return pipeline_store.save_pipeline(_pipelines_dir(), data, expected_name=name)


@api_v1_bp.route("/pipelines", methods=["GET"])
@api_key_required
def pipelines_list():
    folder_filter = request.args.get("folder")
    out = []
    for defn in pipeline_store.list_pipelines(_pipelines_dir()):
        if folder_filter is not None and defn["folder"] != folder_filter.strip():
            continue
        latest = get_db().latest_pipeline_run(defn["name"])
        out.append({**defn, "last_run": pipeline_engine.describe_run(latest) if latest else None})
    return api_response(data={"pipelines": out})


@api_v1_bp.route("/pipelines", methods=["POST"])
@api_key_required
def pipelines_create():
    """Create a pipeline from a YAML body (Content-Type: application/yaml) or JSON."""
    try:
        data, _ = _parse_body()
        name = data.get("name") if isinstance(data, dict) else None
        if isinstance(name, str) and pipeline_store.pipeline_exists(_pipelines_dir(), name):
            return api_response(error_code="DUPLICATE_NAME", error_message=f"Pipeline '{name}' already exists",
                                status_code=409)
        defn = _save_body(None)
    except PipelineValidationError as e:
        return _invalid_pipeline(e)
    return api_response(data={"pipeline": defn}, status_code=201)


@api_v1_bp.route("/pipelines/validate", methods=["POST"])
@api_key_required
def pipelines_validate():
    """Validate a YAML or JSON definition.

    Besides the verdict, returns what an editor needs to switch views: `parsed` (the mapping as
    given, even when invalid), `definition` (normalized, when valid) and, for JSON bodies,
    `yaml` (that mapping as YAML, valid or not).
    """
    try:
        data, text = _parse_body()
    except PipelineValidationError as e:
        return api_response(data={"valid": False, "errors": e.errors, "normalized_yaml": None,
                                  "parsed": None, "definition": None, "yaml": None})
    draft = draft_yaml(data) if text is None and isinstance(data, dict) else None
    try:
        defn = validate_pipeline(data)
    except PipelineValidationError as e:
        return api_response(data={"valid": False, "errors": e.errors, "normalized_yaml": None,
                                  "parsed": data, "definition": None, "yaml": draft})
    return api_response(data={"valid": True, "errors": [], "normalized_yaml": to_yaml(defn),
                              "parsed": data, "definition": defn, "yaml": draft})


@api_v1_bp.route("/pipelines/<name>", methods=["GET"])
@api_key_required
def pipelines_get(name):
    _require_pipeline(name)
    try:
        defn, text = pipeline_store.load_pipeline(_pipelines_dir(), name)
    except PipelineValidationError as e:
        return _invalid_pipeline(e)
    return api_response(data={"pipeline": defn, "yaml": text})


@api_v1_bp.route("/pipelines/<name>", methods=["PUT"])
@api_key_required
def pipelines_update(name):
    _require_pipeline(name)
    try:
        defn = _save_body(name)
    except PipelineValidationError as e:
        return _invalid_pipeline(e)
    return api_response(data={"pipeline": defn})


@api_v1_bp.route("/pipelines/<name>", methods=["DELETE"])
@api_key_required
def pipelines_delete(name):
    _require_pipeline(name)
    try:
        pipeline_engine.delete_pipeline(_pipelines_dir(), name)
    except pipeline_engine.PipelineError as e:
        return _pipeline_error(e)
    return api_response(data={"deleted": True})


@api_v1_bp.route("/pipelines/<name>/runs", methods=["POST"])
@api_key_required
def pipeline_runs_start(name):
    _require_pipeline(name)
    body = request.get_json(silent=True)
    if body is None:
        body = {}
    branches = body.get("branches") if isinstance(body, dict) else body
    if branches is not None and (not isinstance(branches, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in branches.items())):
        return api_response(error_code="INVALID_OVERRIDE",
                            error_message="branches must map step names to branch names",
                            status_code=400)
    try:
        result = pipeline_engine.start_pipeline_run(
            current_app.config["PROJECTS_DIR"], _pipelines_dir(), name, branches=branches)
    except pipeline_engine.PipelineError as e:
        return _pipeline_error(e)
    except PipelineValidationError as e:
        return _invalid_pipeline(e)
    return api_response(data=result, status_code=201)


@api_v1_bp.route("/pipelines/<name>/runs", methods=["GET"])
@api_key_required
def pipeline_runs_list(name):
    _require_pipeline(name)
    limit = request.args.get("limit", 20, type=int)
    runs = get_db().list_pipeline_runs(name, limit=max(1, min(limit, 200)))
    return api_response(data={"runs": [pipeline_engine.describe_run(r) for r in runs]})


@api_v1_bp.route("/pipelines/<name>/runs/latest", methods=["GET"])
@api_key_required
def pipeline_runs_latest(name):
    """The latest run that actually started (queued runs and runs removed from the queue are skipped)."""
    _require_pipeline(name)
    run = get_db().latest_pipeline_run(name)
    if not run:
        return api_response(error_code="NOT_FOUND", error_message=f"{name} has no started runs", status_code=404)
    return api_response(data=pipeline_engine.describe_run(run))


@api_v1_bp.route("/pipelines/<name>/runs/<int:run_id>", methods=["GET"])
@api_key_required
def pipeline_runs_get(name, run_id):
    run = get_db().get_pipeline_run(name, run_id)
    if not run:
        return api_response(error_code="NOT_FOUND", error_message=f"{name}#{run_id} not found", status_code=404)
    return api_response(data=pipeline_engine.describe_run(run))


@api_v1_bp.route("/pipelines/<name>/runs/<int:run_id>/<action>", methods=["POST"])
@api_key_required
def pipeline_runs_action(name, run_id, action):
    handlers = {"cancel": pipeline_engine.cancel_pipeline_run, "resume": pipeline_engine.resume_pipeline_run,
                "dismiss": pipeline_engine.dismiss_pipeline_run}
    if action not in handlers:
        return api_response(error_code="NOT_FOUND", error_message=f"Unknown action '{action}'", status_code=404)
    kwargs = {}
    if action == "resume":
        body = request.get_json(silent=True)
        if body is not None and not isinstance(body, dict):
            return api_response(error_code="INVALID_STEP", error_message="from_step must be a step name",
                                status_code=400)
        if body is not None and "from_step" in body:
            if not isinstance(body["from_step"], str):
                return api_response(error_code="INVALID_STEP", error_message="from_step must be a step name",
                                    status_code=400)
            kwargs["from_step"] = body["from_step"]
    try:
        result = handlers[action](current_app.config["PROJECTS_DIR"], _pipelines_dir(), name, run_id, **kwargs)
    except pipeline_engine.PipelineError as e:
        return _pipeline_error(e)
    return api_response(data=result)


@api_v1_bp.route("/pipelines/<name>/runs/<int:run_id>/steps/<step_name>/log", methods=["GET"])
@api_key_required
def pipeline_step_log(name, run_id, step_name):
    run = get_db().get_pipeline_run(name, run_id)
    if not run:
        return api_response(error_code="NOT_FOUND", error_message=f"{name}#{run_id} not found", status_code=404)
    step = next((s for s in get_db().list_step_runs(run["id"]) if s["step_name"] == step_name), None)
    if not step or not step["log_path"]:
        return api_response(error_code="NOT_FOUND", error_message=f"No log for step '{step_name}'", status_code=404)
    path = os.path.realpath(os.path.join(_pipelines_dir(), step["log_path"]))
    if not path.startswith(os.path.realpath(_pipelines_dir()) + os.sep) or not os.path.isfile(path):
        return api_response(error_code="NOT_FOUND", error_message="Log file missing", status_code=404)
    tail = request.args.get("tail", 500, type=int)
    with open(path, errors="replace") as f:
        lines = f.readlines()
    return api_response(data={"log": "".join(lines[-tail:] if tail > 0 else lines)})


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

def _valid_env(env) -> bool:
    return isinstance(env, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())


def _queue_entry_view(entry: dict, position: int) -> dict:
    return {
        "queue_id": entry["id"], "position": position, "branch": entry["branch"],
        "env_vars": entry["env_vars"], "source": entry["source"],
        "wait_reason": entry["wait_reason"], "created_at": entry["created_at"],
    }


@api_v1_bp.route("/projects/<name>/training/start", methods=["POST"])
@api_key_required
def training_start(name):
    """Start a run now, or with {"queue": true} add it to the project's run queue."""
    load_project(name)
    projects_dir = current_app.config["PROJECTS_DIR"]
    body = request.get_json() or {}
    branch = body.get("branch")  # None -> use project default
    env_vars = body.get("env_vars")
    if branch is not None and not valid_branch_name(branch):
        return api_response(error_code="INVALID_BRANCH",
                            error_message="branch is not a valid branch name", status_code=400)
    if env_vars is not None and not _valid_env(env_vars):
        return api_response(error_code="INVALID_ENV_VARS",
                            error_message="env_vars must map strings to strings", status_code=400)
    if body.get("queue"):
        queue_id = get_db().enqueue_run(name, branch=branch, env_vars=env_vars or None, source="manual")
        position = get_db().queue_position(queue_id)
        return api_response(data={"queue_id": queue_id, "position": position, "status": "queued"},
                            status_code=202)
    result = start_training(projects_dir, name, branch=branch, extra_env=env_vars or None)
    if "error" in result:
        resp, status = api_response(error_code="START_ERROR", error_message=result["error"], status_code=400)
        payload = resp.get_json()
        payload["error"]["details"] = {"code": result.get("code")}
        return jsonify(payload), status
    return api_response(data=result)


@api_v1_bp.route("/projects/<name>/training/can-start", methods=["GET"])
@api_key_required
def training_can_start(name):
    """Side-effect-free training start preflight for UI admission controls."""
    load_project(name)
    return api_response(data=can_start_training(current_app.config["PROJECTS_DIR"], name))


@api_v1_bp.route("/projects/<name>/queue", methods=["GET"])
@api_key_required
def queue_list(name):
    """Queued runs for this project, in the order they will start."""
    load_project(name)
    entries = get_db().list_queue(name)
    return api_response(data={"queue": [_queue_entry_view(e, i + 1) for i, e in enumerate(entries)]})


@api_v1_bp.route("/projects/<name>/queue/<int:queue_id>", methods=["DELETE"])
@api_key_required
def queue_cancel(name, queue_id):
    """Cancel a queued run. Fails with 409 once it has started."""
    load_project(name)
    entry = get_db().get_queue_entry(queue_id)
    if not entry or entry["project_name"] != name:
        return api_response(error_code="NOT_FOUND", error_message=f"Queue entry {queue_id} not found",
                            status_code=404)
    if not get_db().cancel_queue_entry(queue_id, error="cancelled by user"):
        return api_response(error_code="NOT_QUEUED",
                            error_message=f"Queue entry {queue_id} is {entry['status']}", status_code=409)
    return api_response(data={"cancelled": True})


@api_v1_bp.route("/projects/<name>/training/stop", methods=["POST"])
@api_key_required
def training_stop(name):
    """Stop training. Provide run_id in body when multiple runs are active."""
    load_project(name)
    projects_dir = current_app.config["PROJECTS_DIR"]
    body = request.get_json() or {}
    run_id = body.get("run_id")
    result = stop_training(projects_dir, name, run_id=run_id)
    if "error" in result:
        return api_response(error_code="STOP_ERROR", error_message=result["error"], status_code=400)
    return api_response(data=result)


@api_v1_bp.route("/projects/<name>/training/status", methods=["GET"])
@api_key_required
def training_status(name):
    """Get active training runs for a project."""
    load_project(name)
    runs = get_runs_for_project(name)
    status = get_training_status(name)
    return api_response(data={"runs": runs, "tb_port": status.get("tb_port")})


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

@api_v1_bp.route("/projects/<name>/logs", methods=["GET"])
@api_key_required
def get_logs(name):
    """Get log content. Use ?tail=N for last N lines, ?run_id=N for a specific run."""
    load_project(name)

    projects_dir = current_app.config["PROJECTS_DIR"]
    run_id = request.args.get("run_id", type=int)

    if run_id is not None:
        from services.db_service import get_db
        run = get_db().get_run(name, run_id)
        if not run:
            return api_response(error_code="NOT_FOUND",
                                error_message=f"Run {run_id} not found", status_code=404)
        # Prefer archived log; fall back to live log path (handles active parallel runs)
        if run.get('log_file_path'):
            log_path = os.path.join(projects_dir, name, run['log_file_path'])
        else:
            log_path = get_run_log_path(projects_dir, name, run_id=run_id)
    else:
        log_path = get_run_log_path(projects_dir, name)

    if not os.path.isfile(log_path):
        return api_response(data={"content": "", "lines": 0})

    tail = request.args.get("tail", type=int)

    try:
        if tail:
            offset = _tail_offset(log_path, tail)
            with open(log_path, "r") as f:
                f.seek(offset)
                content = f.read()
        else:
            with open(log_path, "r") as f:
                content = f.read()

        lines = content.count("\n")
        return api_response(data={"content": content, "lines": lines})

    except Exception as e:
        return api_response(
            error_code="READ_ERROR",
            error_message=f"Failed to read log file: {e}",
            status_code=500
        )


def _read_log_chunk(log_path, offset):
    if not os.path.isfile(log_path):
        return offset, ""
    size = os.path.getsize(log_path)
    if size < offset:
        offset = 0
    if size <= offset:
        return offset, ""
    with open(log_path, "r") as log_file:
        log_file.seek(offset)
        chunk = log_file.read()
        return log_file.tell(), chunk


def _requested_run_is_active(name, run_id):
    runs = get_runs_for_project(name)
    if run_id is None:
        return bool(runs)
    return any(run["run_id"] == run_id for run in runs)


def _stream_log_events(name, run_id, log_path, tail):
    offset = _tail_offset(log_path, tail) if tail and os.path.isfile(log_path) else 0
    retries_without_data = 0
    while retries_without_data <= 300:
        try:
            offset, chunk = _read_log_chunk(log_path, offset)
            if chunk:
                for line in chunk.splitlines(True):
                    yield f"data: {line.rstrip()}\n\n"
                retries_without_data = 0
                continue
            retries_without_data += 1
            if not _requested_run_is_active(name, run_id) and retries_without_data > 2:
                yield "data: \n\nevent: done\ndata: finished\n\n"
                return
        except Exception:  # NOSONAR - keep the stream alive; errors don't count as idle polls
            pass
        time.sleep(0.5)


@api_v1_bp.route("/projects/<name>/logs/stream", methods=["GET"])
@api_key_required
def stream_logs(name):
    """SSE stream of log content."""
    load_project(name)
    projects_dir = current_app.config["PROJECTS_DIR"]
    tail = request.args.get("tail", type=int)
    run_id = request.args.get("run_id", type=int)
    log_path = get_run_log_path(projects_dir, name, run_id=run_id)

    return Response(_stream_log_events(name, run_id, log_path, tail), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _analysis_log_path(projects_dir, name, run_id):
    if run_id is None:
        return get_run_log_path(projects_dir, name), None
    run = get_db().get_run(name, run_id)
    if not run:
        return None, api_response(
            error_code="NOT_FOUND", error_message=f"Run {run_id} not found", status_code=404)
    if run.get('log_file_path'):
        return os.path.join(projects_dir, name, run['log_file_path']), None
    return get_run_log_path(projects_dir, name, run_id=run_id), None


def _parse_episodes(content):
    episodes = []
    for line in content.split('\n'):
        match = EPISODE_PATTERN.search(line)
        if match:
            episodes.append({
                'episode': int(match.group(1)),
                'reward': float(match.group(2)),
                'epsilon': float(match.group(3)) if match.group(3) else None,
                'steps': int(match.group(4)) if match.group(4) else None,
            })
    return episodes


def _episode_quartiles(episodes):
    if len(episodes) < 4:
        return []
    size = len(episodes) // 4
    quartiles = []
    for index in range(4):
        start = index * size
        end = start + size if index < 3 else len(episodes)
        group = episodes[start:end]
        rewards = [episode['reward'] for episode in group]
        quartiles.append({
            'quartile': index + 1,
            'episode_range': [group[0]['episode'], group[-1]['episode']],
            'count': len(rewards), 'avg_reward': round(sum(rewards) / len(rewards), 2),
            'min_reward': min(rewards), 'max_reward': max(rewards),
        })
    return quartiles


def _episode_trend(quartiles):
    if len(quartiles) < 2:
        return 'insufficient_data'
    first_avg, last_avg = quartiles[0]['avg_reward'], quartiles[-1]['avg_reward']
    if last_avg > first_avg + 0.5:
        return 'improving'
    if last_avg < first_avg - 0.5:
        return 'declining'
    return 'stable'


def _episode_analysis(episodes):
    rewards = [episode['reward'] for episode in episodes]
    recent = episodes[-20:] if len(episodes) >= 20 else episodes
    recent_rewards = [episode['reward'] for episode in recent]
    quartiles = _episode_quartiles(episodes)
    return {
        'parser': 'episode_pipe_v1',
        'parser_warning': 'This endpoint uses a format-specific parser (Episode N | reward: X). For format-agnostic log access use GET /api/v1/projects/{name}/runs/{run_id}/logs',
        'episode_range': [episodes[0]['episode'], episodes[-1]['episode']],
        'total_episodes': len(rewards), 'trend': _episode_trend(quartiles),
        'overall': {
            'avg_reward': round(sum(rewards) / len(rewards), 2),
            'min_reward': min(rewards), 'max_reward': max(rewards),
        },
        'recent': {
            'episode_range': [recent[0]['episode'], recent[-1]['episode']],
            'count': len(recent),
            'avg_reward': round(sum(recent_rewards) / len(recent_rewards), 2),
            'min_reward': min(recent_rewards), 'max_reward': max(recent_rewards),
        },
        'quartiles': quartiles, 'latest_episode': episodes[-1],
    }


@api_v1_bp.route("/projects/<name>/logs/analysis", methods=["GET"])
@api_key_required
def analyze_logs(name):
    """
    DEPRECATED: Format-specific log parser. Only works for logs using the
    "Episode N | reward: X | ..." format. Kept for backward compatibility.

    Prefer GET /api/v1/projects/{name}/runs/{run_id}/logs for raw log access
    that works regardless of log format.

    Query params:
      ?tail=N - Only analyze last N lines (default: 500)
      ?run_id=N - Analyze log for a specific historical run
    """
    load_project(name)
    projects_dir = current_app.config["PROJECTS_DIR"]
    run_id = request.args.get("run_id", type=int)
    log_path, error = _analysis_log_path(projects_dir, name, run_id)
    if error is not None:
        return error
    if not os.path.isfile(log_path):
        return api_response(
            error_code="NO_LOGS",
            error_message="No log file found",
            status_code=404
        )

    tail = request.args.get("tail", default=500, type=int)

    try:
        offset = _tail_offset(log_path, tail)
        with open(log_path, "r") as f:
            f.seek(offset)
            content = f.read()
    except Exception as e:
        return api_response(
            error_code="READ_ERROR",
            error_message=f"Failed to read log file: {e}",
            status_code=500
        )
    episodes = _parse_episodes(content)
    if not episodes:
        return api_response(
            error_code="NO_EPISODES",
            error_message="No episode data found in logs",
            status_code=404
        )
    return api_response(data=_episode_analysis(episodes))


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def _directory_entries(target, items, subpath):
    entries = []
    for item in items:
        if item.startswith(".") or item == "__pycache__":
            continue
        full = os.path.join(target, item)
        relative = os.path.join(subpath, item) if subpath else item
        stat = os.stat(full)
        if os.path.isdir(full):
            entries.append({
                "name": item, "type": "dir", "path": relative,
                "size": None, "size_h": None, "mtime": stat.st_mtime,
            })
        else:
            entries.append({
                "name": item, "type": "file", "path": relative,
                "size": stat.st_size, "size_h": _fmt_size(stat.st_size), "mtime": stat.st_mtime,
            })
    entries.sort(key=lambda entry: (0 if entry["type"] == "dir" else 1, entry["name"].lower()))
    return entries

@api_v1_bp.route("/projects/<name>/files", methods=["GET"])
@api_v1_bp.route("/projects/<name>/files/<path:subpath>", methods=["GET"])
@api_key_required
def browse_files(name, subpath=""):
    """List files in workspace root or subdir, or download a file."""
    load_project(name)

    projects_dir = current_app.config["PROJECTS_DIR"]
    _, target = _safe_path(projects_dir, name, subpath)

    if target is None:
        return api_response(
            error_code="FORBIDDEN",
            error_message="Path traversal not allowed",
            status_code=403
        )

    if not os.path.exists(target):
        return api_response(
            error_code="NOT_FOUND",
            error_message=f"Path not found: {subpath or '/'}",
            status_code=404
        )

    # Download a file directly
    if os.path.isfile(target):
        from flask import send_file
        return send_file(target, as_attachment=True)

    # Zip download for a directory
    if request.args.get("zip") == "1":
        return _zip_directory(target, subpath or name)

    try:
        items = sorted(os.listdir(target))
    except PermissionError:
        return api_response(
            error_code="FORBIDDEN",
            error_message="Permission denied",
            status_code=403
        )
    entries = _directory_entries(target, items, subpath)

    return api_response(data={
        "path": subpath or "",
        "entries": entries,
    })


# ---------------------------------------------------------------------------
# System Stats
# ---------------------------------------------------------------------------

@api_v1_bp.route("/version", methods=["GET"])
def get_version():
    """Return server version and minimum required MCP version."""
    return api_response(data={
        "server_version": SERVER_VERSION,
        "min_mcp_version": MIN_MCP_VERSION,
    })


@api_v1_bp.route("/stats", methods=["GET"])
@api_key_required
def system_stats():
    """Get system stats (CPU, RAM, GPU)."""
    stats = get_all_stats()
    return api_response(data=stats)


@api_v1_bp.route("/busy", methods=["GET"])
@api_key_required
def check_busy():
    """
    Check if any training or project setup is running (useful before
    deploying/restarting — a service restart kills setup's background thread
    mid-clone/install just as it would a training run).

    Returns:
      {
        "busy": true/false,
        "running_projects": ["project1", "project2"],
        "setting_up_projects": ["project3"],
        "running_tasks": ["pipeline#1:step"]
      }
    """
    projects_dir = current_app.config["PROJECTS_DIR"]
    running_projects = []
    setting_up_projects = []

    if os.path.isdir(projects_dir):
        for name in os.listdir(projects_dir):
            running, setting_up = _project_activity(projects_dir, name)
            if running:
                running_projects.append(name)
            if setting_up:
                setting_up_projects.append(name)

    running_tasks = pipeline_engine.running_task_labels()

    return api_response(data={
        "busy": bool(running_projects or setting_up_projects or running_tasks),
        "running_projects": running_projects,
        "setting_up_projects": setting_up_projects,
        "running_tasks": running_tasks,
    })


def _project_activity(projects_dir, name):
    config_path = os.path.join(projects_dir, name, PROJECT_FILE)
    if not os.path.isfile(config_path):
        return False, False
    running = get_training_status(name).get("status") == "running"
    try:
        project = Project.load(config_path)
    except Exception:
        return running, False
    return running, project.setup_status in SETUP_ACTIVE_STATUSES


@api_v1_bp.route("/capacity", methods=["GET"])
@api_key_required
def get_capacity():
    """System-wide training capacity: total slots, running, available."""
    projects_dir = current_app.config["PROJECTS_DIR"]
    total_slots = 0
    total_running = 0
    project_list = []

    if os.path.isdir(projects_dir):
        for proj_name in sorted(os.listdir(projects_dir)):
            config_path = os.path.join(projects_dir, proj_name, PROJECT_FILE)
            if not os.path.isfile(config_path):
                continue
            try:
                project = Project.load(config_path)
            except Exception:
                continue
            max_runs = project.max_parallel_runs if project.parallel_runs_enabled else 1
            running_count = len(get_runs_for_project(proj_name))
            total_slots += max_runs
            total_running += running_count
            project_list.append({
                "name": proj_name,
                "running_runs": running_count,
                "max_runs": max_runs,
            })

    return api_response(data={
        "total_slots": total_slots,
        "running": total_running,
        "available": total_slots - total_running,
        "projects": project_list,
        "cpu": get_cpu_stats(),
        "memory": get_memory_stats(),
        "gpus": get_gpu_stats(),
        "gpu_platform": get_gpu_platform_info(),
    })


# ---------------------------------------------------------------------------
# Run History
# ---------------------------------------------------------------------------

@api_v1_bp.route("/projects/<name>/runs", methods=["GET"])
@api_key_required
def list_runs(name):
    """List training run history."""
    load_project(name)

    from services.db_service import get_db
    runs = get_db().get_training_runs(name, limit=20)

    return api_response(data={"runs": [public_run(r) for r in runs]})


@api_v1_bp.route("/projects/<name>/runs/<int:run_id>", methods=["GET"])
@api_key_required
def get_run(name, run_id):
    """Get details for a specific run."""
    load_project(name)

    from services.db_service import get_db
    run = get_db().get_run(name, run_id)

    if not run:
        return api_response(
            error_code="NOT_FOUND",
            error_message=f"Run {run_id} not found",
            status_code=404
        )

    return api_response(data={"run": public_run(run)})


@api_v1_bp.route("/projects/<name>/runs/<int:run_id>/log", methods=["GET"])
@api_key_required
def download_run_log(name, run_id):
    """Download archived log for a run."""
    load_project(name)

    from services.db_service import get_db
    run = get_db().get_run(name, run_id)

    if not run:
        return api_response(
            error_code="NOT_FOUND",
            error_message=f"Run {run_id} not found",
            status_code=404
        )

    if not run.get('log_file_path'):
        return api_response(
            error_code="NOT_FOUND",
            error_message="Log file not available",
            status_code=404
        )

    projects_dir = current_app.config["PROJECTS_DIR"]
    log_path = os.path.join(projects_dir, name, run['log_file_path'])

    if not os.path.isfile(log_path):
        return api_response(
            error_code="NOT_FOUND",
            error_message="Log file not found on disk",
            status_code=404
        )

    from flask import send_file
    return send_file(log_path, as_attachment=True,
                     download_name=f"{run_ref(name, run_id)}.log")


@api_v1_bp.route("/projects/<name>/runs/<int:run_id>/logs", methods=["GET"])
@api_key_required
def get_run_logs_json(name, run_id):
    """Get log content as JSON for a specific run."""
    load_project(name)

    from services.db_service import get_db
    run = get_db().get_run(name, run_id)

    if not run:
        return api_response(
            error_code="NOT_FOUND",
            error_message=f"Run {run_id} not found",
            status_code=404
        )

    projects_dir = current_app.config["PROJECTS_DIR"]

    if run.get('log_file_path'):
        log_relative_path = run['log_file_path']
        log_path = os.path.join(projects_dir, name, log_relative_path)
    else:
        # Run is still active — log_file_path is only written to DB on completion
        log_path = get_run_log_path(projects_dir, name, run_id)
        log_relative_path = os.path.relpath(log_path, os.path.join(projects_dir, name))

    if not os.path.isfile(log_path):
        return api_response(
            error_code="NO_LOGS",
            error_message="Log file not found",
            status_code=404
        )

    # Query params
    tail_lines = request.args.get("tail_lines", default=200, type=int)
    start_byte = request.args.get("start_byte", type=int)
    limit_bytes = request.args.get("limit_bytes", default=65536, type=int)

    try:
        file_size = os.path.getsize(log_path)

        if start_byte is None:
            actual_start = _tail_offset(log_path, tail_lines)
        else:
            actual_start = max(0, min(start_byte, file_size))

        with open(log_path, "rb") as f:
            f.seek(actual_start)
            content_bytes = f.read(limit_bytes)
            actual_end = f.tell()

        content = content_bytes.decode('utf-8', errors='replace')

        return api_response(data={
            'run_id': run_id,
            'run_ref': run_ref(name, run_id),
            'path': log_relative_path,
            'content': content,
            'encoding': 'utf-8',
            'start_byte': actual_start,
            'end_byte': actual_end,
            'bytes_returned': len(content_bytes),
            'truncated_before': actual_start > 0,
            'truncated_after': actual_end < file_size,
            'line_count': content.count('\n')
        })

    except Exception as e:
        return api_response(
            error_code="READ_ERROR",
            error_message=f"Failed to read log file: {e}",
            status_code=500
        )


@api_v1_bp.route("/projects/<name>/runs/<run_id_str>/files", methods=["GET"])
@api_v1_bp.route("/projects/<name>/runs/<run_id_str>/files/<path:subpath>", methods=["GET"])
@api_key_required
def run_files(name, run_id_str, subpath=""):
    """Browse or download files from a specific run's persistent storage.

    run_id_str may be an integer run id or 'latest'. Use this instead of the
    project-level /projects/<name>/files endpoint when fetching artifacts —
    that endpoint resolves against the current workspace symlink, which is
    shared and can be repointed by other runs (including parallel ones).
    """
    load_project(name)

    projects_dir = current_app.config["PROJECTS_DIR"]
    run = _resolve_run(projects_dir, name, run_id_str)
    if run is None:
        return api_response(
            error_code="NOT_FOUND",
            error_message=f"No run with artifacts found for '{run_id_str}'",
            status_code=404
        )

    run_id = run["run_number"]
    _, target = _safe_run_path(projects_dir, name, run, subpath)
    if target is None:
        return api_response(
            error_code="FORBIDDEN",
            error_message="Path traversal not allowed",
            status_code=403
        )

    if not os.path.exists(target):
        return api_response(
            error_code="NOT_FOUND",
            error_message=f"Path not found: {subpath or '/'}",
            status_code=404
        )

    if os.path.isfile(target):
        from flask import send_file
        return send_file(target, as_attachment=True)

    if request.args.get("zip") == "1":
        label = subpath.replace("/", "-") if subpath else run_ref(name, run_id)
        return _zip_directory(target, label)

    try:
        items = sorted(os.listdir(target))
    except PermissionError:
        return api_response(
            error_code="FORBIDDEN",
            error_message="Permission denied",
            status_code=403
        )
    entries = _directory_entries(target, items, subpath)

    return api_response(data={
        "run_id": run_id,
        "run_ref": run_ref(name, run_id),
        "path": subpath or "",
        "entries": entries,
    })


@api_v1_bp.route("/projects/<name>/runs/clear", methods=["DELETE"])
@api_key_required
def clear_all_runs(name):
    """Clear all run history."""
    load_project(name)

    # Delegate to training route handler logic
    from routes.training import clear_history
    return clear_history(name)


@api_v1_bp.route("/projects/<name>/runs/cleanup-orphaned", methods=["POST"])
@api_key_required
def cleanup_orphaned_runs(name):
    """
    Mark orphaned runs as 'canceled'.

    Orphaned runs are those stuck in 'running' status but the process
    is no longer active (e.g., after a server restart).
    """
    load_project(name)

    from services.db_service import get_db
    from services.process_manager import get_training_status

    db = get_db()
    runs = db.get_training_runs(name, limit=100)

    # Find runs marked as running
    running_runs = [r for r in runs if r['status'] == 'running']

    # Check if training is actually running
    status = get_training_status(name)
    actual_running = status.get('status') == 'running'

    cleaned = 0
    for run in running_runs:
        # If nothing is actually running, or this run is old, mark as canceled
        if not actual_running:
            db.update_training_run(
                run['id'],
                status='canceled',
                ended_at=run['started_at'],  # Use start time as end since we don't know
                duration_seconds=0
            )
            cleaned += 1

    return api_response(data={
        "cleaned": cleaned,
        "message": f"Marked {cleaned} orphaned run(s) as canceled"
    })


def _auto_metric_run(db, name):
    runs = db.get_training_runs(name, limit=100)
    if not runs:
        return None, api_response(
            error_code="NO_RUNS", error_message="No training runs found", status_code=404)
    running = [run for run in runs if run['status'] == 'running']
    if len(running) > 1:
        ids = [run['run_number'] for run in running]
        return None, api_response(
            error_code="MULTIPLE_RUNS_ACTIVE",
            error_message=f"Multiple runs are active (IDs: {ids}). Specify ?run_id=N to select one.",
            status_code=409,
        )
    if running:
        return running[0], None
    for statuses in ({'completed'}, {'canceled', 'crashed'}):
        matching = [run for run in runs if run['status'] in statuses]
        if matching:
            return matching[0], None
    return None, api_response(
        error_code="NO_USABLE_RUNS",
        error_message="No training runs found with usable status",
        status_code=404,
    )


def _metric_run(db, name, requested_run_id):
    if requested_run_id is None:
        return _auto_metric_run(db, name)
    run = db.get_run(name, requested_run_id)
    if run:
        return run, None
    return None, api_response(
        error_code="NOT_FOUND", error_message=f"Run {requested_run_id} not found", status_code=404)


@api_v1_bp.route("/projects/<name>/tensorboard/latest", methods=["GET"])
@api_key_required
def get_latest_metrics(name):
    """
    Get metrics analysis for the current or most recent run.

    Prioritizes the currently running run if one exists, otherwise
    returns the most recent completed run.

    Query params:
      ?detail=low (default, summary only) | medium (+ samples) | high (all data)
      ?metrics=loss,accuracy (filter specific metrics)
      ?run_id=N (analyze a specific run instead of auto-selecting latest)
    """
    from flask import request
    from services import tensorboard_service
    from services.db_service import get_db

    load_project(name)

    # Get query parameters
    detail = request.args.get('detail', 'low')
    if detail not in ['low', 'medium', 'high']:
        return api_response(
            error_code="INVALID_PARAMETER",
            error_message="detail must be 'low', 'medium', or 'high'",
            status_code=400
        )

    metrics_param = request.args.get('metrics')
    metric_filter = [m.strip() for m in metrics_param.split(',')] if metrics_param else None

    db = get_db()
    target_run, error = _metric_run(db, name, request.args.get('run_id', type=int))
    if error is not None:
        return error
    is_active = target_run['status'] == 'running'

    run_pk = target_run['id']

    # Active runs: always re-parse — TFEvents grow as training progresses
    if is_active:
        db.delete_metric_analyses(run_pk)

    # Get metrics analysis
    projects_dir = current_app.config["PROJECTS_DIR"]
    result = tensorboard_service.get_metric_analysis(
        projects_dir, name, run_pk, metric_filter, detail
    )

    if 'error' in result:
        return api_response(
            error_code=result['error'],
            error_message=result['message'],
            status_code=404
        )

    # Build response
    response_data = {
        'run_id': target_run['run_number'],
        'run_ref': run_ref(name, target_run['run_number']),
        'is_active': is_active,
        'run_info': {
            'started_at': target_run['started_at'],
            'ended_at': target_run['ended_at'],
            'status': target_run['status'],
            'duration_seconds': target_run['duration_seconds']
        },
        'metrics': result['metrics']
    }

    return api_response(data=response_data)


@api_v1_bp.route("/projects/<name>/tensorboard/cleanup", methods=["POST"])
@api_key_required
def cleanup_tensorboard_logs(name):
    """
    Cleanup old TensorBoard log directories, keeping only N most recent runs.

    Request body:
      {
        "keep_count": 10,  // Number of recent runs to keep
        "cleanup_run_history": true  // Also cleanup old run records from database (default: true)
      }
    """
    from services.tensorboard_service import cleanup_old_tb_logs

    project = load_project(name)

    data = request.get_json() or {}
    keep_count = data.get("keep_count")
    cleanup_db = data.get("cleanup_run_history", True)

    if not keep_count or not isinstance(keep_count, int) or keep_count < 1:
        return api_response(
            error_code="INVALID_KEEP_COUNT",
            error_message="keep_count must be a positive integer",
            status_code=400
        )

    projects_dir = current_app.config["PROJECTS_DIR"]
    workspace_dir = os.path.join(projects_dir, name, "workspace")
    tb_logdir = os.path.join(workspace_dir, project.tensorboard_log_dir)

    # Cleanup TensorBoard logs
    result = cleanup_old_tb_logs(tb_logdir, keep_count)

    # Cleanup old run history if requested
    db_cleanup_info = None
    if cleanup_db:
        from services.db_service import get_db
        db = get_db()
        runs = db.get_training_runs(name, limit=1000)

        # Sort by started_at descending (newest first)
        runs.sort(key=lambda r: r['started_at'], reverse=True)

        # Delete runs beyond keep_count
        deleted_run_ids = []
        for run in runs[keep_count:]:
            delete_run_storage(projects_dir, name, run)
            db.delete_training_run(run['id'])
            deleted_run_ids.append(run['run_number'])

        db_cleanup_info = {
            'deleted_count': len(deleted_run_ids),
            'kept_count': min(len(runs), keep_count)
        }

    response = {
        'tensorboard': result,
        'run_history': db_cleanup_info
    }

    return api_response(data=response)


# ---------------------------------------------------------------------------
# API Documentation
# ---------------------------------------------------------------------------

@api_v1_bp.route("/docs", methods=["GET"])
def api_documentation():
    """Render the API documentation page."""
    from flask import render_template
    return render_template("api_docs.html")


@api_v1_bp.route("/mcp", methods=["GET"])
def mcp_documentation():
    """Render the MCP setup guide."""
    from flask import render_template
    beekeeper_home = current_app.config["BEEKEEPER_HOME"]
    mcp_server_path = os.path.join(beekeeper_home, MCP_SERVER_FILE)
    return render_template("mcp.html", mcp_server_path=mcp_server_path)


@api_v1_bp.route("/mcp/server", methods=["GET"])
def download_mcp_server():
    """Download mcp_server.py."""
    from flask import send_file
    beekeeper_home = current_app.config["BEEKEEPER_HOME"]
    path = os.path.join(beekeeper_home, MCP_SERVER_FILE)
    return send_file(path, as_attachment=True, download_name=MCP_SERVER_FILE, mimetype="text/x-python")


# ---------------------------------------------------------------------------
# Agent Instructions
# ---------------------------------------------------------------------------

def _agent_instruction_projects(projects_dir):
    projects = []
    if not os.path.isdir(projects_dir):
        return projects
    for name in sorted(os.listdir(projects_dir)):
        config_path = os.path.join(projects_dir, name, PROJECT_FILE)
        if not os.path.isfile(config_path):
            continue
        try:
            project = Project.load(config_path)
            status = get_training_status(name)
        except Exception:
            continue
        projects.append({
            "name": project.name, "branch": project.branch,
            "setup_status": project.setup_status, "train_status": status["status"],
        })
    return projects


def _agent_project_table(projects):
    if not projects:
        return "(no projects found)"
    lines = []
    for project in projects:
        indicator = "▶" if project["train_status"] == "running" else " "
        lines.append(
            f"{indicator} {project['name']:<30} setup={project['setup_status']:<8} "
            f"train={project['train_status']:<10} branch={project['branch']}"
        )
    return "\n".join(lines)

@api_v1_bp.route("/agent/instructions", methods=["GET"])
@api_key_required
def get_global_agent_instructions():
    """
    Global agent instructions — orientation for an agent using the Beekeeper MCP server.
    Lists all current projects and explains how to get started.
    """
    host = request.host

    projects_dir = current_app.config["PROJECTS_DIR"]
    project_table = _agent_project_table(_agent_instruction_projects(projects_dir))

    content = f"""# Beekeeper — Agent Instructions

You are connected to a Beekeeper ML training manager via the **Beekeeper MCP server**.
Use the MCP tools below for all operations.

> **If anything here seems stale, re-fetch this document:**
> `GET http://{host}/api/v1/agent/instructions`

## What is Beekeeper?

Beekeeper manages long-running ML training jobs: git clone, environment setup,
training controls, log streaming, and TensorBoard. Everything runs on a remote GPU server.

## Step 1 — Orient Yourself

Current projects on this instance:

```
{project_table}
```

Use `get_project(<name>)` for detail on any project.
Use `get_project_instructions(<name>)` to load full project context before working with it.

## Step 2 — Load Project Context

Before analyzing or managing a project, call both:

```
get_project_instructions(<name>)   # full context: purpose, metrics, workflows
analyze_run(<name>)                # synthesized TensorBoard + log metrics
```

Read both outputs before taking any action.

## MCP Tools Reference

| Tool | Description |
|------|-------------|
| `list_projects(folder=None)` | All projects and status; optionally filter by folder |
| `list_folders()` | Folder names used by projects and pipelines |
| `rename_folder(folder, new_name)` | Rename a folder across projects and pipelines |
| `get_project(name)` | Detailed project info |
| `create_project(name, git_url, ...)` | Create project (setup runs in background) |
| `update_project(name, ...)` | Update project settings or folder |
| `retry_setup(name)` | Retry failed setup, polls until done |
| `delete_project(name)` | Delete project and all data |
| `get_project_instructions(name)` | Full project-specific agent context |
| `start_training(name, queue=False)` | Start training or add it to the run queue |
| `list_queue(name)` | List queued runs and their wait reasons |
| `cancel_queued_run(name, queue_id)` | Remove a run that has not started |
| `stop_training(name)` | Stop training |
| `training_status(name)` | Current training status |
| `get_logs(name, tail)` | Raw log output (default: last 100 lines) |
| `analyze_run(name)` | TensorBoard metrics + episode log trends |
| `list_pipelines(folder=None)` | Pipelines and their latest runs |
| `get_pipeline(name)` | Parsed pipeline definition and raw YAML |
| `create_pipeline(definition_yaml)` | Create a YAML pipeline |
| `validate_pipeline(definition_yaml)` | Validate YAML without saving it |
| `update_pipeline(name, definition_yaml)` | Replace a pipeline definition |
| `delete_pipeline(name)` | Delete a pipeline and pipeline run history |
| `start_pipeline(name)` | Start a pipeline run |
| `list_pipeline_runs(name, limit=20)` | Recent pipeline runs, newest first |
| `pipeline_status(name, run_id=None)` | Latest or specified pipeline run and steps |
| `cancel_pipeline(name, run_id)` | Cancel a run and stop its current step |
| `resume_pipeline(name, run_id, from_step=None)` | Resume a failed/cancelled run, or re-run the latest stopped run from a named step |
| `dismiss_pipeline_run(name, run_id)` | Skip a failed run so the queue continues |
| `get_step_log(name, run_id, step_name, tail=200)` | Task-step log output |
| `list_branches(name)` | Available branches |
| `switch_branch(name, branch)` | Switch branch (training must be stopped) |
| `get_stats()` | GPU, CPU, RAM stats |
| `check_busy()` | Is any training currently running? |

`cancel_pipeline` reports the run's true terminal status. If the run finishes while
the cancellation is being processed, its response status may be `succeeded` or `failed`
instead of `cancelled`.

## Artifact Storage

Every training run automatically gets a persistent directory outside the git workspace:

```
projects/<project>/persistent/runs/run_<id>/
```

Beekeeper sets these environment variables in every training process:

| Variable | Points to |
|----------|-----------|
| `BEEKEEPER_RUN_DIR` | The run's persistent directory (absolute path) |
| `BEEKEEPER_RUN_ID` | The per-project run number (`run_id`), e.g. `3` |
| `BEEKEEPER_RUN_REF` | The run's human-readable ref, `<project>-<run_id>`, e.g. `myproject-3` |
| `BEEKEEPER_TENSORBOARD_DIR` | Same — canonical location for TensorBoard events |
| `TENSORBOARD_LOG_DIR` | Same — generic alias for frameworks that read this |

Training scripts should write to `$BEEKEEPER_RUN_DIR` (or use relative paths via
the workspace symlinks Beekeeper creates) rather than hardcoded local paths.
Example: `checkpoint_dir = os.environ.get("BEEKEEPER_RUN_DIR", "checkpoints")`.

Additional workspace paths to protect (e.g. `saved_models`, `exports`) are set
on the project via `output_paths`. Each listed path gets a workspace symlink pointing
into the run's persistent directory, so code that writes relative paths still works.
Use `get_project(<name>)` to see the current `output_paths` for a project.
TensorBoard logs are always protected — do not add the TB log dir to `output_paths`.

Run artifacts survive until the run record is explicitly deleted. When you see
`persistent_dir` on a run in `get_project()` history, that is the project-relative
path to its durable artifact tree.

## Common Workflows

### Check system state
```
check_busy()       # anything running?
get_stats()        # GPU util, VRAM, CPU, RAM
list_projects()    # all projects and status
```

### Analyze training
```
training_status(<name>)
analyze_run(<name>)
get_logs(<name>, tail=100)
```

> **Raw logs are for error and crash diagnosis only.** Do not characterize episode reward
> trends from `get_logs` output — RL episode variance is too high for a windowed tail sample
> to be meaningful. For trend assessment, use `analyze_run` (TensorBoard-backed).

### Start training
```
check_busy()                      # confirm nothing else is running
training_status(<name>)           # verify idle
start_training(<name>)
```

### Switch branches
```
training_status(<name>)           # must be idle
list_branches(<name>)
switch_branch(<name>, <branch>)
```

### Create a new project
```
create_project(<name>, <git_url>, output_paths=["saved_models", "exports"])
# Setup runs in background — retry_setup polls until ready:
retry_setup(<name>)
start_training(<name>)
```

Defaults: branch=main, python_version=3.12, train_file=train.py, env_type=venv.
output_paths defaults to [] — TensorBoard is always protected regardless.
"""

    return Response(
        content,
        mimetype="text/markdown",
        headers={
            "Content-Disposition": "attachment; filename=BEEKEEPER.md"
        }
    )


@api_v1_bp.route("/projects/<name>/agent/instructions", methods=["GET"])
@api_key_required
def get_agent_instructions(name):
    """
    Project-specific agent instructions for use with the Beekeeper MCP server.
    """
    load_project(name)

    content = f"""# Beekeeper: {name}

You are connected to Beekeeper via the **MCP server**. Use MCP tools for all operations.

> **If this seems stale, call `get_project_instructions("{name}")` to refresh.**

## Step 1 — Get current state

```
training_status("{name}")
analyze_run("{name}")
```

## Before Your First Analysis — Ask This Once

Ask the user:

> "What is the primary metric I should treat as the performance signal for this project,
> and is higher or lower better? For example: `Train/episode_reward` (higher is better),
> or `val_loss` (lower is better)."

Remember the answer. This determines what counts as a good run.

---

## MCP Tools for This Project

| Tool | Description |
|------|-------------|
| `training_status("{name}")` | Current training status |
| `start_training("{name}", queue=False)` | Start training or queue it |
| `list_queue("{name}")` | List this project's queued runs |
| `cancel_queued_run("{name}", queue_id)` | Cancel a queued run |
| `stop_training("{name}")` | Stop training |
| `analyze_run("{name}")` | TensorBoard metrics + episode log trends |
| `get_logs("{name}", tail=100)` | Raw log output |
| `list_branches("{name}")` | Available branches |
| `switch_branch("{name}", branch)` | Switch branch (training must be stopped) |
| `get_project("{name}")` | Project config details |
| `list_folders()` | Folder names used by projects and pipelines |
| `list_pipelines(folder=None)` | Pipelines and latest runs |
| `get_pipeline(name)` | Pipeline definition and YAML |
| `create_pipeline(definition_yaml)` | Create a pipeline from YAML |
| `validate_pipeline(definition_yaml)` | Validate YAML without saving it |
| `update_pipeline(name, definition_yaml)` | Replace pipeline YAML |
| `delete_pipeline(name)` | Delete a pipeline and its pipeline run history |
| `start_pipeline(name)` | Start a pipeline run |
| `list_pipeline_runs(name, limit=20)` | Recent pipeline runs, newest first |
| `pipeline_status(name, run_id=None)` | Pipeline and step status |
| `cancel_pipeline(name, run_id)` | Cancel a pipeline run |
| `resume_pipeline(name, run_id, from_step=None)` | Resume a stopped run or re-run it from a named step |
| `dismiss_pipeline_run(name, run_id)` | Skip a failed run so the queue continues |
| `get_step_log(name, run_id, step_name)` | Read a task-step log |
| `get_stats()` | GPU, CPU, RAM |
| `check_busy()` | Is any training running? |

If a pipeline finishes while `cancel_pipeline` is being processed, the response preserves
the truth: its status may be `succeeded` or `failed`, not `cancelled`.

## Workflows

### Analyze a running or completed run
```
analyze_run("{name}")          # TensorBoard metrics + episode trends
get_logs("{name}", tail=100)   # raw logs for errors or debug messages
```

`analyze_run` combines TensorBoard and log-based episode analysis. If TensorBoard hasn't
flushed yet, the log-based section still works.

> **Raw logs are for error and crash diagnosis only.** Do not characterize episode reward
> trends from `get_logs` output — RL episode variance is too high for a windowed tail sample
> to be meaningful. For trend assessment, always use `analyze_run` (TensorBoard-backed).

### Start training
```
check_busy()                   # confirm nothing else is running
training_status("{name}")      # verify idle
start_training("{name}")
```

### Stop training
```
training_status("{name}")      # verify it's running
stop_training("{name}")
```

### Switch branches
```
training_status("{name}")      # must be idle
list_branches("{name}")
switch_branch("{name}", branch)
```

## Understanding `analyze_run` Output

Returns two sections: TensorBoard metrics and episode log analysis.

**Which run does it analyze?**
- Training **running** → current active run
- Training **idle** → most recent completed run

**Key fields in each metric:**

| Field | Meaning |
|-------|---------|
| `trend` | Overall trend: `improving`, `stable`, `worsening`, or `unstable` |
| `recent_trend` | Trend of the last 20% of steps — may differ from overall |
| `improvement_percent` | Change from start to end |
| `peak_value` | Best value reached at any point |
| `peak_step` | Step at which the peak occurred |
| `peak_reversal_pct` | How far the metric dropped from its peak, as % of total range |
| `converged` | Has the metric stabilized? |
| `convergence_step` | Step where convergence was detected |
| `anomaly_count` | Number of unusual spikes or drops |

**Interpreting trends:**
- `improving`: Moving in the expected direction (loss ↓, reward ↑)
- `stable`: Leveled off — convergence or plateau
- `worsening`: Confidently moving the wrong direction — needs attention
- `unstable`: High variance, no clear direction

## In-Depth Analysis Guide

**1. Identify the primary performance metric first.**
Look for: `reward`, `score`, `return`, `accuracy`, `success_rate`, `win_rate`.
This is the headline. Everything else explains *why* it looks the way it does.

**2. Scan for critical signals before summarizing.**
Priority order:
- `trend: worsening` on any metric — actively degrading
- `peak_reversal_pct > 50` on a performance metric — peaked and significantly reversed
- `recent_trend` differs from `trend` — run changed direction in the final stretch
- `peak_step` very early relative to total steps — peaked early and never recovered
- High `anomaly_count` — spikes or instability

**3. Separate metric categories.**
- **Performance** (`reward`, `score`, `accuracy`): "is the agent improving?"
- **Loss** (`q_loss`, `td_error`, `reconstruction_loss`): "is the model learning correctly?"
- **Schedule** (`epsilon`, `lr`, `temperature`): intentional decays — not performance signals
- ⚠️ Improving losses with declining reward = model optimizing the wrong objective.

**4. Use `peak_reversal_pct` and `peak_step` to tell the full story.**
- `> 20%`: worth mentioning
- `> 50%`: red flag — report peak value, peak step, and current value
- `> 80%`: run largely reversed — likely the primary finding

**5. Lead with the performance verdict, use losses to explain it.**
Don't open with "reconstruction loss improved 99%." Open with where the reward/score stands,
then use losses to explain why.

## Artifact Storage

Every run stores its outputs in a persistent directory that survives workspace cleanup:

```
projects/{name}/persistent/runs/run_<id>/
```

Beekeeper injects these env vars into every training process:

| Variable | Value |
|----------|-------|
| `BEEKEEPER_RUN_DIR` | Absolute path to the run's persistent directory |
| `BEEKEEPER_RUN_ID` | Per-project run number (`run_id`) |
| `BEEKEEPER_RUN_REF` | `<project>-<run_id>`, how to refer to the run |
| `BEEKEEPER_TENSORBOARD_DIR` | Same — canonical TensorBoard event location |
| `TENSORBOARD_LOG_DIR` | Same — generic alias |

If a user asks where their model weights, checkpoints, or exports are:
1. Call `get_project("{name}")` and check `run_history` for the relevant run's `run_id`.
   `run_id` is the per-project run number (1, 2, 3, ...). When talking to a person, refer
   to a run by its `run_ref` (`<project>-<run_id>`, e.g. `{name}-3`), never by a bare number.
2. Fetch it with `GET /api/v1/projects/{name}/runs/<run_id>/files/<path>` — use `latest`
   in place of `<run_id>` if no specific run was requested. Add `?zip=1` to download a
   directory as a zip instead of listing it.
3. Do NOT use `GET /api/v1/projects/{name}/files/<path>` for artifacts — that endpoint
   resolves against the current workspace symlink, which is shared project-wide and can
   be repointed by other runs (including parallel ones), so it can silently serve the
   wrong run's data or 404 once a run's workspace is cleaned up. It's for browsing the
   live workspace only (source code, configs), not for fetching run outputs.

If a user asks how their training script should write checkpoints or outputs:
- Recommend using `os.environ.get("BEEKEEPER_RUN_DIR", ".")` as the base path.
- Alternatively, add the target directory to `output_paths` in project settings — Beekeeper
  will create a workspace symlink so relative-path writes are automatically redirected there.

The project's configured `output_paths` field (visible in `get_project("{name}")`) lists
which workspace-relative directories are symlinked into persistent storage. TensorBoard
logs are always protected regardless of `output_paths`.
"""

    return Response(
        content,
        mimetype="text/markdown",
        headers={
            "Content-Disposition": f"attachment; filename=BEEKEEPER_{name}.md"
        }
    )


# ---------------------------------------------------------------------------
# Branch Management
# ---------------------------------------------------------------------------

@api_v1_bp.route("/projects/<name>/branches", methods=["GET"])
@api_key_required
def list_branches(name):
    """List available branches from the remote repository."""
    project = load_project(name)

    if not project.git_url:
        return api_response(
            error_code="NO_GIT_URL",
            error_message="Project has no git URL configured",
            status_code=400
        )

    try:
        # Get remote branches
        result = subprocess.run(
            ["git", "ls-remote", "--heads", project.git_url],
            capture_output=True,
            text=True,
            timeout=30,
            env=git_env(),
        )

        if result.returncode != 0:
            return api_response(
                error_code="GIT_ERROR",
                error_message=f"Failed to list remote branches: {result.stderr.strip()}",
                status_code=500
            )

        # Parse branch names from output
        # Format: <sha>\trefs/heads/<branch>
        branches = []
        for line in result.stdout.strip().split("\n"):
            if line and "\t" in line:
                ref = line.split("\t")[1]
                if ref.startswith("refs/heads/"):
                    branch_name = ref[len("refs/heads/"):]
                    branches.append(branch_name)

        branches.sort()

        return api_response(data={
            "branches": branches,
            "current": project.branch
        })

    except subprocess.TimeoutExpired:
        return api_response(
            error_code="TIMEOUT",
            error_message="Timed out fetching branches from remote",
            status_code=504
        )
    except Exception as e:
        return api_response(
            error_code="ERROR",
            error_message=str(e),
            status_code=500
        )


@api_v1_bp.route("/projects/<name>/branch", methods=["POST"])
@api_key_required
def switch_branch(name):
    """Switch to a different branch."""
    project = load_project(name)

    # Block if any run is active (starting or running)
    from services.process_manager import get_runs_for_project
    if get_runs_for_project(name):
        return api_response(
            error_code="TRAINING_RUNNING",
            error_message="Cannot switch branches while training is active",
            status_code=409
        )

    # Get requested branch
    data = request.get_json() or {}
    new_branch = data.get("branch")
    if not new_branch:
        return api_response(
            error_code="MISSING_BRANCH",
            error_message="Branch name is required",
            status_code=400
        )

    if new_branch == project.branch:
        return api_response(data={
            "branch": new_branch,
            "status": "already_on_branch"
        })

    projects_dir = current_app.config["PROJECTS_DIR"]
    workspace_dir = os.path.join(projects_dir, name, "workspace")

    if not os.path.isdir(workspace_dir):
        return api_response(
            error_code="NO_WORKSPACE",
            error_message="Project workspace does not exist",
            status_code=400
        )

    try:
        # Fetch from origin
        fetch_result = subprocess.run(
            ["git", "fetch", "origin"],
            cwd=workspace_dir,
            capture_output=True,
            text=True,
            timeout=60,
            env=git_env(),
        )

        if fetch_result.returncode != 0:
            return api_response(
                error_code="FETCH_FAILED",
                error_message=f"Failed to fetch: {fetch_result.stderr.strip()}",
                status_code=500
            )

        # Checkout the branch, discarding any local workspace changes
        checkout_result = subprocess.run(
            ["git", "checkout", "-f", new_branch],
            cwd=workspace_dir,
            capture_output=True,
            text=True,
            timeout=30
        )

        # If local branch doesn't exist, track remote
        if checkout_result.returncode != 0:
            checkout_result = subprocess.run(
                ["git", "checkout", "-f", "--track", f"origin/{new_branch}"],
                cwd=workspace_dir,
                capture_output=True,
                text=True,
                timeout=30
            )

        if checkout_result.returncode != 0:
            return api_response(
                error_code="CHECKOUT_FAILED",
                error_message=f"Failed to checkout: {checkout_result.stderr.strip()}",
                status_code=500
            )

        # Hard-reset to remote — remote is always authoritative
        reset_result = subprocess.run(
            ["git", "reset", "--hard", f"origin/{new_branch}"],
            cwd=workspace_dir,
            capture_output=True,
            text=True,
            timeout=30
        )
        if reset_result.returncode != 0:
            return api_response(
                error_code="RESET_FAILED",
                error_message=f"Failed to reset to remote: {reset_result.stderr.strip()}",
                status_code=500
            )

        # Update project.json with new branch
        project.branch = new_branch
        project.save(projects_dir)

        return api_response(data={
            "branch": new_branch,
            "status": "switched"
        })

    except subprocess.TimeoutExpired:
        return api_response(
            error_code="TIMEOUT",
            error_message="Git operation timed out",
            status_code=504
        )
    except Exception as e:
        return api_response(
            error_code="ERROR",
            error_message=str(e),
            status_code=500
        )


@api_v1_bp.route("/projects/<name>/runs/<int:run_id>/metrics", methods=["GET"])
@api_key_required
def get_run_metrics(name, run_id):
    """
    Get metrics for specific run.

    Query params:
      ?detail=low (default, summary only) | medium (+ samples) | high (all data)
      ?metrics=loss,accuracy (filter specific metrics)
    """
    from flask import request
    from services import tensorboard_service
    from services.db_service import get_db

    load_project(name)

    # Get query parameters
    detail = request.args.get('detail', 'low')
    if detail not in ['low', 'medium', 'high']:
        return api_response(
            error_code="INVALID_PARAMETER",
            error_message="detail must be 'low', 'medium', or 'high'",
            status_code=400
        )

    metrics_param = request.args.get('metrics')
    metric_filter = [m.strip() for m in metrics_param.split(',')] if metrics_param else None

    # Verify run exists
    db = get_db()
    run = db.get_run(name, run_id)

    if not run:
        return api_response(
            error_code="RUN_NOT_FOUND",
            error_message=f"Run {run_id} not found",
            status_code=404
        )

    # Get metrics analysis
    projects_dir = current_app.config["PROJECTS_DIR"]
    result = tensorboard_service.get_metric_analysis(
        projects_dir, name, run["id"], metric_filter, detail
    )

    if 'error' in result:
        return api_response(
            error_code=result['error'],
            error_message=result['message'],
            status_code=404
        )

    # Build response
    response_data = {
        'run_id': run['run_number'],
        'run_ref': run_ref(name, run['run_number']),
        'run_info': {
            'started_at': run['started_at'],
            'ended_at': run['ended_at'],
            'status': run['status'],
            'duration_seconds': run['duration_seconds']
        },
        'metrics': result['metrics']
    }

    return api_response(data=response_data)


@api_v1_bp.route("/projects/<name>/agent/sdk", methods=["GET"])
@api_key_required
def download_agent_sdk(name):
    """Generate and download Python SDK for AI agent integration."""
    from services.agent_sdk_generator import generate_sdk
    from flask import Response

    project = load_project(name)

    # Generate SDK content
    sdk_content = generate_sdk(
        project_name=name,
        base_url=request.url_root.rstrip('/'),
        project=project
    )

    # Return as downloadable file
    filename = f"beekeeper_client_{name.replace('-', '_')}.py"
    return Response(
        sdk_content,
        mimetype='text/x-python',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"'
        }
    )
