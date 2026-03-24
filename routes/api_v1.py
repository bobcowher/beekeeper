"""
REST API v1 for Beekeeper.

All endpoints return JSON with a consistent envelope:
    {"success": true, "data": {...}}
    {"success": false, "error": {"code": "...", "message": "..."}}
"""

import os
import time
from flask import Blueprint, current_app, jsonify, request, Response

from models.project import Project
from services.process_manager import start_training, stop_training, get_training_status
from services.stats_service import get_all_stats
from services.auth_service import api_key_required

# Reuse helpers from existing routes
from routes.training import _tail_offset
from routes.files import _safe_path, _fmt_size, _zip_directory

api_v1_bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")


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


def load_project(name):
    """Load a project by name, returning (Project, None) or (None, error_response)."""
    projects_dir = current_app.config["PROJECTS_DIR"]
    config_path = os.path.join(projects_dir, name, "project.json")
    if not os.path.isfile(config_path):
        return None, api_response(
            error_code="NOT_FOUND",
            error_message=f"Project '{name}' not found",
            status_code=404
        )
    try:
        project = Project.load(config_path)
        return project, None
    except Exception as e:
        return None, api_response(
            error_code="LOAD_ERROR",
            error_message=f"Failed to load project: {e}",
            status_code=500
        )


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@api_v1_bp.route("/projects")
@api_key_required
def list_projects():
    """List all projects with basic info and training status."""
    projects_dir = current_app.config["PROJECTS_DIR"]
    projects = []

    if not os.path.isdir(projects_dir):
        return api_response(data={"projects": []})

    for name in sorted(os.listdir(projects_dir)):
        config_path = os.path.join(projects_dir, name, "project.json")
        if os.path.isfile(config_path):
            try:
                project = Project.load(config_path)
                status = get_training_status(name)
                projects.append({
                    "name": project.name,
                    "git_url": project.git_url,
                    "branch": project.branch,
                    "setup_status": project.setup_status,
                    "train_status": status["status"],
                    "pinned": project.pinned,
                    "last_run_at": project.last_run_at,
                })
            except Exception:
                continue

    return api_response(data={"projects": projects})


@api_v1_bp.route("/projects/<name>")
@api_key_required
def get_project(name):
    """Get detailed project info including training status."""
    project, error = load_project(name)
    if error:
        return error

    status = get_training_status(name)

    return api_response(data={
        "project": {
            **project.to_dict(),
            "training": status,
        }
    })


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

@api_v1_bp.route("/projects/<name>/training/start", methods=["POST"])
@api_key_required
def training_start(name):
    """Start training for a project."""
    project, error = load_project(name)
    if error:
        return error

    if project.setup_status != "ready":
        return api_response(
            error_code="SETUP_INCOMPLETE",
            error_message="Project setup is not complete",
            status_code=400
        )

    # Check if already running
    status = get_training_status(name)
    if status["status"] == "running":
        return api_response(
            error_code="ALREADY_RUNNING",
            error_message="Training is already running",
            status_code=409
        )

    projects_dir = current_app.config["PROJECTS_DIR"]
    result = start_training(projects_dir, name)

    if "error" in result:
        return api_response(
            error_code="START_FAILED",
            error_message=result["error"],
            status_code=400
        )

    return api_response(data={
        "status": "started",
        "pid": result.get("pid"),
        "tb_port": result.get("tb_port"),
    })


@api_v1_bp.route("/projects/<name>/training/stop", methods=["POST"])
@api_key_required
def training_stop(name):
    """Stop training for a project."""
    project, error = load_project(name)
    if error:
        return error

    # Check if running
    status = get_training_status(name)
    if status["status"] != "running":
        return api_response(
            error_code="NOT_RUNNING",
            error_message="Training is not running",
            status_code=409
        )

    projects_dir = current_app.config["PROJECTS_DIR"]
    result = stop_training(projects_dir, name)

    if "error" in result:
        return api_response(
            error_code="STOP_FAILED",
            error_message=result["error"],
            status_code=400
        )

    return api_response(data={"status": "stopped"})


@api_v1_bp.route("/projects/<name>/training/status")
@api_key_required
def training_status(name):
    """Get training status for a project."""
    project, error = load_project(name)
    if error:
        return error

    status = get_training_status(name)
    return api_response(data=status)


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

@api_v1_bp.route("/projects/<name>/logs")
@api_key_required
def get_logs(name):
    """Get log content. Use ?tail=N for last N lines."""
    project, error = load_project(name)
    if error:
        return error

    projects_dir = current_app.config["PROJECTS_DIR"]
    log_path = os.path.join(projects_dir, name, "train.log")

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


@api_v1_bp.route("/projects/<name>/logs/stream")
@api_key_required
def stream_logs(name):
    """SSE stream of log content."""
    project, error = load_project(name)
    if error:
        return error

    projects_dir = current_app.config["PROJECTS_DIR"]
    log_path = os.path.join(projects_dir, name, "train.log")

    tail = request.args.get("tail", type=int)

    def generate():
        if tail and os.path.isfile(log_path):
            offset = _tail_offset(log_path, tail)
        else:
            offset = 0

        retries_without_data = 0
        max_idle = 300  # stop after 5 min of no data and no running process

        while True:
            try:
                if os.path.isfile(log_path):
                    size = os.path.getsize(log_path)
                    if size < offset:
                        # Log file was truncated/rewritten (new run)
                        offset = 0
                    if size > offset:
                        with open(log_path, "r") as f:
                            f.seek(offset)
                            chunk = f.read()
                            offset = f.tell()
                        if chunk:
                            for line in chunk.splitlines(True):
                                yield f"data: {line.rstrip()}\n\n"
                            retries_without_data = 0
                            continue

                retries_without_data += 1
                info = get_training_status(name)
                if info["status"] != "running" and retries_without_data > 2:
                    yield "data: \n\nevent: done\ndata: finished\n\n"
                    return
                if retries_without_data > max_idle:
                    return

            except Exception:
                pass

            time.sleep(0.5)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

@api_v1_bp.route("/projects/<name>/files")
@api_v1_bp.route("/projects/<name>/files/<path:subpath>")
@api_key_required
def browse_files(name, subpath=""):
    """List files in workspace root or subdir, or download a file."""
    project, error = load_project(name)
    if error:
        return error

    projects_dir = current_app.config["PROJECTS_DIR"]
    workspace_dir, target = _safe_path(projects_dir, name, subpath)

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

    # List directory contents
    entries = []
    try:
        items = sorted(os.listdir(target))
    except PermissionError:
        return api_response(
            error_code="FORBIDDEN",
            error_message="Permission denied",
            status_code=403
        )

    for item in items:
        # Skip hidden files and __pycache__
        if item.startswith(".") or item == "__pycache__":
            continue
        full = os.path.join(target, item)
        rel = os.path.join(subpath, item) if subpath else item
        if os.path.isdir(full):
            entries.append({
                "name": item,
                "type": "dir",
                "path": rel,
                "size": None,
                "size_h": None,
            })
        else:
            sz = os.path.getsize(full)
            entries.append({
                "name": item,
                "type": "file",
                "path": rel,
                "size": sz,
                "size_h": _fmt_size(sz),
            })

    # Sort: dirs first, then files
    entries.sort(key=lambda e: (0 if e["type"] == "dir" else 1, e["name"].lower()))

    return api_response(data={
        "path": subpath or "",
        "entries": entries,
    })


# ---------------------------------------------------------------------------
# System Stats
# ---------------------------------------------------------------------------

@api_v1_bp.route("/stats")
@api_key_required
def system_stats():
    """Get system stats (CPU, RAM, GPU)."""
    stats = get_all_stats()
    return api_response(data=stats)


# ---------------------------------------------------------------------------
# Run History
# ---------------------------------------------------------------------------

@api_v1_bp.route("/projects/<name>/runs")
@api_key_required
def list_runs(name):
    """List training run history."""
    project, error = load_project(name)
    if error:
        return error

    from services.db_service import get_db
    runs = get_db().get_training_runs(name, limit=20)

    return api_response(data={"runs": runs})


@api_v1_bp.route("/projects/<name>/runs/<int:run_id>")
@api_key_required
def get_run(name, run_id):
    """Get details for a specific run."""
    project, error = load_project(name)
    if error:
        return error

    from services.db_service import get_db
    run = get_db().get_training_run(run_id)

    if not run or run['project_name'] != name:
        return api_response(
            error_code="NOT_FOUND",
            error_message=f"Run {run_id} not found",
            status_code=404
        )

    return api_response(data={"run": run})


@api_v1_bp.route("/projects/<name>/runs/<int:run_id>/log")
@api_key_required
def download_run_log(name, run_id):
    """Download archived log for a run."""
    project, error = load_project(name)
    if error:
        return error

    from services.db_service import get_db
    run = get_db().get_training_run(run_id)

    if not run or run['project_name'] != name:
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
    return send_file(log_path, as_attachment=True)


@api_v1_bp.route("/projects/<name>/runs/clear", methods=["DELETE"])
@api_key_required
def clear_all_runs(name):
    """Clear all run history."""
    project, error = load_project(name)
    if error:
        return error

    # Delegate to training route handler logic
    from routes.training import clear_history
    return clear_history(name)


@api_v1_bp.route("/projects/<name>/tensorboard/latest")
@api_key_required
def get_latest_metrics(name):
    """
    Get metrics analysis for most recent completed run.

    Query params:
      ?detail=low (default, summary only) | medium (+ samples) | high (all data)
      ?metrics=loss,accuracy (filter specific metrics)
    """
    from flask import request
    from services import tensorboard_service
    from services.db_service import get_db

    project, error = load_project(name)
    if error:
        return error

    # Get query parameters
    detail = request.args.get('detail', 'low')
    if detail not in ['low', 'medium', 'high']:
        return api_response(
            error_code="INVALID_PARAMETER",
            error_message="detail must be 'low', 'medium', or 'high'",
            status_code=400
        )

    metric_filter = None
    if request.args.get('metrics'):
        metric_filter = [m.strip() for m in request.args.get('metrics').split(',')]

    # Get latest completed run
    db = get_db()
    runs = db.get_training_runs(name, limit=100)
    completed_runs = [r for r in runs if r['status'] == 'completed']

    if not completed_runs:
        return api_response(
            error_code="NO_COMPLETED_RUNS",
            error_message="No completed training runs found",
            status_code=404
        )

    latest_run = completed_runs[0]
    run_id = latest_run['id']

    # Get metrics analysis
    projects_dir = current_app.config["PROJECTS_DIR"]
    result = tensorboard_service.get_metric_analysis(
        projects_dir, name, run_id, metric_filter, detail
    )

    if 'error' in result:
        return api_response(
            error_code=result['error'],
            error_message=result['message'],
            status_code=404
        )

    # Build response
    response_data = {
        'run_id': run_id,
        'run_info': {
            'started_at': latest_run['started_at'],
            'ended_at': latest_run['ended_at'],
            'status': latest_run['status'],
            'duration_seconds': latest_run['duration_seconds']
        },
        'metrics': result['metrics']
    }

    return api_response(data=response_data)


@api_v1_bp.route("/projects/<name>/runs/<int:run_id>/metrics")
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

    project, error = load_project(name)
    if error:
        return error

    # Get query parameters
    detail = request.args.get('detail', 'low')
    if detail not in ['low', 'medium', 'high']:
        return api_response(
            error_code="INVALID_PARAMETER",
            error_message="detail must be 'low', 'medium', or 'high'",
            status_code=400
        )

    metric_filter = None
    if request.args.get('metrics'):
        metric_filter = [m.strip() for m in request.args.get('metrics').split(',')]

    # Verify run exists
    db = get_db()
    run = db.get_training_run(run_id)

    if not run:
        return api_response(
            error_code="RUN_NOT_FOUND",
            error_message=f"Run {run_id} not found",
            status_code=404
        )

    if run['project_name'] != name:
        return api_response(
            error_code="RUN_NOT_FOUND",
            error_message=f"Run {run_id} does not belong to project {name}",
            status_code=404
        )

    # Get metrics analysis
    projects_dir = current_app.config["PROJECTS_DIR"]
    result = tensorboard_service.get_metric_analysis(
        projects_dir, name, run_id, metric_filter, detail
    )

    if 'error' in result:
        return api_response(
            error_code=result['error'],
            error_message=result['message'],
            status_code=404
        )

    # Build response
    response_data = {
        'run_id': run_id,
        'run_info': {
            'started_at': run['started_at'],
            'ended_at': run['ended_at'],
            'status': run['status'],
            'duration_seconds': run['duration_seconds']
        },
        'metrics': result['metrics']
    }

    return api_response(data=response_data)
