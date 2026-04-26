"""
REST API v1 for Beekeeper.

All endpoints return JSON with a consistent envelope:
    {"success": true, "data": {...}}
    {"success": false, "error": {"code": "...", "message": "..."}}
"""

import os
import subprocess
import time
from typing import NoReturn
from flask import Blueprint, abort, current_app, jsonify, request, Response

from models.project import Project
from services.process_manager import start_training, stop_training, get_training_status
from services.stats_service import get_all_stats
from services.auth_service import api_key_required

# Reuse helpers from existing routes
from routes.training import _tail_offset
from routes.files import _safe_path, _fmt_size, _zip_directory

api_v1_bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")

CLI_VERSION = "1.1.1"
CLI_RELEASE_BASE = "https://github.com/bobcowher/beekeeper-cli/releases/download"


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
    config_path = os.path.join(projects_dir, name, "project.json")
    if not os.path.isfile(config_path):
        _abort_json(404, "NOT_FOUND", f"Project '{name}' not found")
    try:
        return Project.load(config_path)
    except Exception as e:
        _abort_json(500, "LOAD_ERROR", f"Failed to load project: {e}")


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


@api_v1_bp.route("/projects", methods=["POST"])
@api_key_required
def create_project():
    """Create a new project from a git repository."""
    import re
    from services.project_service import create_project as create_project_service

    data = request.get_json() or {}

    # Validate required fields
    name = data.get("name", "").strip()
    if not name:
        return api_response(
            error_code="MISSING_NAME",
            error_message="Project name is required",
            status_code=400
        )

    if not re.match(r"^[a-zA-Z0-9_-]+$", name):
        return api_response(
            error_code="INVALID_NAME",
            error_message="Invalid project name. Use only letters, numbers, hyphens, underscores.",
            status_code=400
        )

    git_url = data.get("git_url", "").strip()
    if not git_url:
        return api_response(
            error_code="MISSING_GIT_URL",
            error_message="Git URL is required",
            status_code=400
        )

    projects_dir = current_app.config["PROJECTS_DIR"]

    # Check for duplicate
    if os.path.exists(os.path.join(projects_dir, name)):
        return api_response(
            error_code="DUPLICATE_NAME",
            error_message=f"Project '{name}' already exists",
            status_code=409
        )

    # Validate data_dir if enabled
    data_dir_enabled = data.get("data_dir_enabled", False)
    if data_dir_enabled:
        data_dir_remote = data.get("data_dir_remote", "").strip()
        if not data_dir_remote:
            return api_response(
                error_code="MISSING_DATA_DIR",
                error_message="System data path is required when data directory is enabled",
                status_code=400
            )
        if not os.path.isdir(data_dir_remote):
            return api_response(
                error_code="INVALID_DATA_DIR",
                error_message=f"System data path '{data_dir_remote}' does not exist or is not a directory",
                status_code=400
            )

    # Build project data with defaults
    project_data = {
        "name": name,
        "git_url": git_url,
        "branch": data.get("branch", "main").strip() or "main",
        "python_version": data.get("python_version", "3.12"),
        "train_file": data.get("train_file", "train.py").strip() or "train.py",
        "tensorboard_log_dir": data.get("tensorboard_log_dir", "runs").strip() or "runs",
        "requirements_file": data.get("requirements_file", "requirements.txt").strip() or "requirements.txt",
        "env_type": data.get("env_type", "venv"),
        "setup_script": data.get("setup_script", "").strip(),
        "data_dir_enabled": data_dir_enabled,
        "data_dir_local": data.get("data_dir_local", "data").strip() or "data",
        "data_dir_remote": data.get("data_dir_remote", "").strip(),
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
    config_path = os.path.join(projects_dir, name, "project.json")
    project = Project.load(config_path)

    return api_response(
        data={"project": project.to_dict()},
        status_code=201
    )


@api_v1_bp.route("/projects/<name>")
@api_key_required
def get_project(name):
    """Get detailed project info including training status."""
    project = load_project(name)

    status = get_training_status(name)

    return api_response(data={
        "project": {
            **project.to_dict(),
            "training": status,
        }
    })


@api_v1_bp.route("/projects/<name>/clone", methods=["POST"])
@api_key_required
def clone_project(name):
    """Clone an existing project with a new name."""
    import re
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

    if not re.match(r"^[a-zA-Z0-9_-]+$", new_name):
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
    config_path = os.path.join(projects_dir, new_name, "project.json")
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

    Stops TensorBoard if running. Cannot delete while training is running —
    stop training first.
    """
    load_project(name)

    status = get_training_status(name)
    if status["status"] == "running":
        return api_response(
            error_code="TRAINING_RUNNING",
            error_message="Cannot delete project while training is running — stop training first",
            status_code=409
        )

    from services.process_manager import stop_tensorboard
    from services.project_service import delete_project
    projects_dir = current_app.config["PROJECTS_DIR"]
    stop_tensorboard(name)
    delete_project(projects_dir, name)

    return api_response(data={"deleted": name})


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

@api_v1_bp.route("/projects/<name>/training/start", methods=["POST"])
@api_key_required
def training_start(name):
    """Start training for a project."""
    project = load_project(name)

    if project.setup_status != "ready":
        return api_response(
            error_code="SETUP_INCOMPLETE",
            error_message="Project setup is not complete",
            status_code=400
        )

    # Check if already running or starting
    status = get_training_status(name)
    if status["status"] in ("running", "starting"):
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

    return api_response(
        data={"status": "starting"},
        status_code=202
    )


@api_v1_bp.route("/projects/<name>/training/stop", methods=["POST"])
@api_key_required
def training_stop(name):
    """Stop training for a project."""
    load_project(name)

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
    load_project(name)

    status = get_training_status(name)
    return api_response(data=status)


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

@api_v1_bp.route("/projects/<name>/logs")
@api_key_required
def get_logs(name):
    """Get log content. Use ?tail=N for last N lines, ?run_id=N for a specific run."""
    load_project(name)

    projects_dir = current_app.config["PROJECTS_DIR"]
    run_id = request.args.get("run_id", type=int)

    if run_id is not None:
        from services.db_service import get_db
        run = get_db().get_training_run(run_id)
        if not run or run['project_name'] != name:
            return api_response(
                error_code="NOT_FOUND",
                error_message=f"Run {run_id} not found",
                status_code=404
            )
        log_file_path = run.get('log_file_path')
        if log_file_path:
            log_path = os.path.join(projects_dir, name, log_file_path)
        else:
            # Active run — fall back to live log
            log_path = os.path.join(projects_dir, name, "train.log")
    else:
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
    load_project(name)

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


@api_v1_bp.route("/projects/<name>/logs/analysis")
@api_key_required
def analyze_logs(name):
    """
    Analyze training logs to extract episode metrics and trends.

    Works for active runs where tensorboard data may not be flushed yet.
    Parses log lines matching: "Episode N | reward: X | ..."

    Query params:
      ?tail=N - Only analyze last N lines (default: 500)
      ?run_id=N - Analyze log for a specific historical run
    """
    import re

    load_project(name)

    projects_dir = current_app.config["PROJECTS_DIR"]
    run_id = request.args.get("run_id", type=int)

    if run_id is not None:
        from services.db_service import get_db
        run = get_db().get_training_run(run_id)
        if not run or run['project_name'] != name:
            return api_response(
                error_code="NOT_FOUND",
                error_message=f"Run {run_id} not found",
                status_code=404
            )
        log_file_path = run.get('log_file_path')
        if log_file_path:
            log_path = os.path.join(projects_dir, name, log_file_path)
        else:
            log_path = os.path.join(projects_dir, name, "train.log")
    else:
        log_path = os.path.join(projects_dir, name, "train.log")

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

    # Parse episode lines - flexible pattern to match common formats
    # "Episode 123 | reward: 4.5 | epsilon: 0.3 | steps: 200"
    episode_pattern = re.compile(
        r'Episode\s+(\d+)\s*\|\s*reward:\s*([-\d.]+)'
        r'(?:\s*\|\s*epsilon:\s*([\d.]+))?'
        r'(?:\s*\|\s*steps:\s*(\d+))?',
        re.IGNORECASE
    )

    episodes = []
    for line in content.split('\n'):
        match = episode_pattern.search(line)
        if match:
            ep_num = int(match.group(1))
            reward = float(match.group(2))
            epsilon = float(match.group(3)) if match.group(3) else None
            steps = int(match.group(4)) if match.group(4) else None
            episodes.append({
                'episode': ep_num,
                'reward': reward,
                'epsilon': epsilon,
                'steps': steps
            })

    if not episodes:
        return api_response(
            error_code="NO_EPISODES",
            error_message="No episode data found in logs",
            status_code=404
        )

    # Compute statistics
    rewards = [ep['reward'] for ep in episodes]
    n = len(rewards)

    # Quartile analysis for trend
    q_size = n // 4 if n >= 4 else n
    quartiles = []
    if n >= 4:
        for i in range(4):
            start = i * q_size
            end = start + q_size if i < 3 else n
            q_rewards = rewards[start:end]
            q_episodes = episodes[start:end]
            quartiles.append({
                'quartile': i + 1,
                'episode_range': [q_episodes[0]['episode'], q_episodes[-1]['episode']],
                'count': len(q_rewards),
                'avg_reward': round(sum(q_rewards) / len(q_rewards), 2),
                'min_reward': min(q_rewards),
                'max_reward': max(q_rewards)
            })

    # Determine trend
    if len(quartiles) >= 2:
        first_avg = quartiles[0]['avg_reward']
        last_avg = quartiles[-1]['avg_reward']
        if last_avg > first_avg + 0.5:
            trend = 'improving'
        elif last_avg < first_avg - 0.5:
            trend = 'declining'
        else:
            trend = 'stable'
    else:
        trend = 'insufficient_data'

    # Recent window (last 20 episodes)
    recent = episodes[-20:] if len(episodes) >= 20 else episodes
    recent_rewards = [ep['reward'] for ep in recent]

    return api_response(data={
        'episode_range': [episodes[0]['episode'], episodes[-1]['episode']],
        'total_episodes': n,
        'trend': trend,
        'overall': {
            'avg_reward': round(sum(rewards) / n, 2),
            'min_reward': min(rewards),
            'max_reward': max(rewards),
        },
        'recent': {
            'episode_range': [recent[0]['episode'], recent[-1]['episode']],
            'count': len(recent),
            'avg_reward': round(sum(recent_rewards) / len(recent_rewards), 2),
            'min_reward': min(recent_rewards),
            'max_reward': max(recent_rewards),
        },
        'quartiles': quartiles,
        'latest_episode': episodes[-1],
    })


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

@api_v1_bp.route("/projects/<name>/files")
@api_v1_bp.route("/projects/<name>/files/<path:subpath>")
@api_key_required
def browse_files(name, subpath=""):
    """List files in workspace root or subdir, or download a file."""
    load_project(name)

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
        stat = os.stat(full)
        if os.path.isdir(full):
            entries.append({
                "name": item,
                "type": "dir",
                "path": rel,
                "size": None,
                "size_h": None,
                "mtime": stat.st_mtime,
            })
        else:
            sz = os.path.getsize(full)
            entries.append({
                "name": item,
                "type": "file",
                "path": rel,
                "size": sz,
                "size_h": _fmt_size(sz),
                "mtime": stat.st_mtime,
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

@api_v1_bp.route("/cli/version")
@api_key_required
def cli_version():
    """Return the CLI version this server expects and the download URL."""
    return api_response(data={
        "cli_version": CLI_VERSION,
        "download_url": f"{CLI_RELEASE_BASE}/v{CLI_VERSION}/beekeeper",
        "download_url_windows": f"{CLI_RELEASE_BASE}/v{CLI_VERSION}/beekeeper.exe",
    })


@api_v1_bp.route("/stats")
@api_key_required
def system_stats():
    """Get system stats (CPU, RAM, GPU)."""
    stats = get_all_stats()
    return api_response(data=stats)


@api_v1_bp.route("/busy")
@api_key_required
def check_busy():
    """
    Check if any training is running (useful before deploying/restarting).

    Returns:
      {
        "busy": true/false,
        "running_projects": ["project1", "project2"]
      }
    """
    projects_dir = current_app.config["PROJECTS_DIR"]
    running_projects = []

    if os.path.isdir(projects_dir):
        for name in os.listdir(projects_dir):
            config_path = os.path.join(projects_dir, name, "project.json")
            if os.path.isfile(config_path):
                status = get_training_status(name)
                if status.get("status") == "running":
                    running_projects.append(name)

    return api_response(data={
        "busy": len(running_projects) > 0,
        "running_projects": running_projects
    })


# ---------------------------------------------------------------------------
# Run History
# ---------------------------------------------------------------------------

@api_v1_bp.route("/projects/<name>/runs")
@api_key_required
def list_runs(name):
    """List training run history."""
    load_project(name)

    from services.db_service import get_db
    runs = get_db().get_training_runs(name, limit=20)

    return api_response(data={"runs": runs})


@api_v1_bp.route("/projects/<name>/runs/<int:run_id>")
@api_key_required
def get_run(name, run_id):
    """Get details for a specific run."""
    load_project(name)

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
    load_project(name)

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


@api_v1_bp.route("/projects/<name>/tensorboard/latest")
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
    requested_run_id = request.args.get('run_id', type=int)

    if requested_run_id is not None:
        # Fetch the specific run requested
        target_run = db.get_training_run(requested_run_id)
        if not target_run or target_run['project_name'] != name:
            return api_response(
                error_code="NOT_FOUND",
                error_message=f"Run {requested_run_id} not found",
                status_code=404
            )
        is_active = target_run['status'] == 'running'
    else:
        # Auto-select: running > completed > canceled/crashed
        runs = db.get_training_runs(name, limit=100)

        if not runs:
            return api_response(
                error_code="NO_RUNS",
                error_message="No training runs found",
                status_code=404
            )

        running_runs = [r for r in runs if r['status'] == 'running']
        completed_runs = [r for r in runs if r['status'] == 'completed']
        other_runs = [r for r in runs if r['status'] in ('canceled', 'crashed')]

        if running_runs:
            target_run = running_runs[0]
            is_active = True
        elif completed_runs:
            target_run = completed_runs[0]
            is_active = False
        elif other_runs:
            target_run = other_runs[0]
            is_active = False
        else:
            return api_response(
                error_code="NO_USABLE_RUNS",
                error_message="No training runs found with usable status",
                status_code=404
            )

    run_id = target_run['id']

    # Active runs: always re-parse — TFEvents grow as training progresses
    if is_active:
        db.delete_metric_analyses(run_id)

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
    if cleanup_db and result['deleted']:
        from services.db_service import get_db
        db = get_db()
        runs = db.get_training_runs(name, limit=1000)

        # Sort by started_at descending (newest first)
        runs.sort(key=lambda r: r['started_at'], reverse=True)

        # Delete runs beyond keep_count
        deleted_run_ids = []
        for run in runs[keep_count:]:
            db.delete_training_run(run['id'])
            deleted_run_ids.append(run['id'])

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

@api_v1_bp.route("/docs")
def api_documentation():
    """Render the API documentation page."""
    from flask import render_template
    return render_template("api_docs.html", cli_version=CLI_VERSION, cli_release_base=CLI_RELEASE_BASE)


@api_v1_bp.route("/cli")
def cli_documentation():
    """Render the CLI documentation page."""
    from flask import render_template
    return render_template("cli.html", cli_version=CLI_VERSION, cli_release_base=CLI_RELEASE_BASE)


# ---------------------------------------------------------------------------
# Agent Instructions
# ---------------------------------------------------------------------------

@api_v1_bp.route("/agent/instructions")
@api_key_required
def get_global_agent_instructions():
    """
    Global agent instructions — orientation for an agent that doesn't yet know
    which project to work with. Lists all projects, explains how to install
    the CLI, and links to project-specific instructions.

    Intended to be fetched once at the start of a session and added to context.
    """
    host = request.host

    # Fetch live project list to embed current state
    projects_dir = current_app.config["PROJECTS_DIR"]
    projects = []
    if os.path.isdir(projects_dir):
        for pname in sorted(os.listdir(projects_dir)):
            config_path = os.path.join(projects_dir, pname, "project.json")
            if os.path.isfile(config_path):
                try:
                    p = Project.load(config_path)
                    status = get_training_status(pname)
                    projects.append({
                        "name": p.name,
                        "branch": p.branch,
                        "setup_status": p.setup_status,
                        "train_status": status["status"],
                        "pinned": p.pinned,
                        "last_run_at": p.last_run_at,
                    })
                except Exception:
                    continue

    # Build project table
    if projects:
        project_lines = []
        for p in projects:
            indicator = "▶" if p["train_status"] == "running" else " "
            project_lines.append(
                f"{indicator} {p['name']:<30} setup={p['setup_status']:<8} train={p['train_status']:<10} branch={p['branch']}"
            )
        project_table = "\n".join(project_lines)
    else:
        project_table = "(no projects found)"

    content = f"""# Beekeeper — Agent Instructions

**Use the `beekeeper` CLI for all operations. Do not call the REST API directly.**

> **If anything in these instructions doesn't make sense or a command doesn't exist,
> this file may be stale. Refresh it by re-fetching:**
> ```bash
> beekeeper version   # also confirms CLI is current
> curl -s http://{host}/api/v1/agent/instructions
> ```
> Reload the output into your context before continuing.

Base URL: http://{host}

## What is Beekeeper?

Beekeeper is an ML training manager — git clone, venv setup, training controls, log streaming, and TensorBoard. You manage it through the `beekeeper` CLI.

## Step 1 — Verify the CLI is installed and current

```bash
beekeeper version
```

- If "command not found" — **stop and tell the user** the CLI needs to be installed. Do not install it yourself. Give them this command to run:
- If "UPDATE REQUIRED" — **stop and tell the user** their CLI is out of date and show them the update command printed in the output.
- If "UP TO DATE" — proceed to Step 2.

Installation command (for user to run if CLI is missing):

```bash
# Linux
curl -L -o /usr/local/bin/beekeeper {CLI_RELEASE_BASE}/v{CLI_VERSION}/beekeeper && chmod +x /usr/local/bin/beekeeper

# Windows
curl -L -o beekeeper.exe {CLI_RELEASE_BASE}/v{CLI_VERSION}/beekeeper.exe
```

CLI version: {CLI_VERSION}

## Step 2 — Configure

```bash
export BEEKEEPER_HOST="http://{host}"
export BEEKEEPER_API_KEY="your-api-key"   # omit if auth is disabled
```

## Step 3 — Orient Yourself

Current projects on this instance:

```
{project_table}
```

If the list is empty, see "Starting from Scratch" below.

For more detail on any project:
```bash
beekeeper projects info <name>
```

## Step 4 — Load Project Context

Before working with a project, fetch its instructions and run an analysis:

```bash
beekeeper projects instructions <name>   # full project context and endpoint reference
beekeeper run analyze <name>             # synthesized metrics + trend analysis
```

Read both before taking any action.

## CLI Reference

```
beekeeper projects list                         List all projects and status
beekeeper projects info <name>                  Detailed project info
beekeeper projects create <name> <git_url>      Create a new project
                       [branch] [python]        Optional: branch (default: main), python version
                       [train_file] [env_type]  Optional: train file, env type (venv|conda)
beekeeper projects instructions <name>          Fetch project-specific agent instructions
beekeeper projects retry <name>                 Retry failed setup (polls until done)
beekeeper projects delete <name>                Delete a project and all its data
beekeeper training start <name>                 Start training
beekeeper training stop <name>                  Stop training
beekeeper training status <name>                Current training status
beekeeper logs get <name> [tail]                Fetch log output (default: last 100 lines)
beekeeper run analyze <name>                    Synthesized metrics + trend analysis
beekeeper branch list <name>                    List available branches
beekeeper branch switch <name> <branch>         Switch to a different branch
beekeeper stats                                 System stats (GPU, CPU, RAM)
beekeeper busy                                  Check if any training is running (exit 1 if busy)
beekeeper version                               Check CLI version against server
```

## Common Workflows

### Check system state before starting work
```bash
beekeeper busy           # is anything running? (exit 1 = busy, exit 0 = idle)
beekeeper stats          # GPU util, VRAM, CPU, RAM
beekeeper projects list  # all projects and their status
```

### Load context for a project
```bash
beekeeper projects instructions <name>
beekeeper run analyze <name>
```

### Analyze and monitor training
```bash
beekeeper training status <name>
beekeeper run analyze <name>
beekeeper logs get <name> 100
```

### Start / stop training
```bash
beekeeper busy                        # confirm nothing else is running first
beekeeper training start <name>
beekeeper training stop <name>
```

### Switch branches
```bash
beekeeper branch list <name>          # see what's available
beekeeper branch switch <name> <branch>
```

### Starting from Scratch

If there are no projects, ask the user for the details you need, then:

```bash
beekeeper projects create <name> <git_url>
# Setup runs automatically in background. Poll until ready:
beekeeper projects info <name>        # repeat until setup_status=ready
# Or use retry which polls for you:
beekeeper projects retry <name>
# Then start training:
beekeeper training start <name>
```

Minimum required: `name` and `git_url`. Defaults: branch=main, python=3.12, train_file=train.py, env_type=venv.

### Retry a failed setup
```bash
beekeeper projects retry <name>
# polls automatically until setup completes or fails
```

### Remove a project
```bash
beekeeper projects delete <name>
```
"""

    return Response(
        content,
        mimetype="text/markdown",
        headers={
            "Content-Disposition": "attachment; filename=BEEKEEPER.md"
        }
    )


@api_v1_bp.route("/projects/<name>/agent/instructions")
@api_key_required
def get_agent_instructions(name):
    """
    Download agent instructions as a markdown file.

    This file can be added to a project's CLAUDE.md or similar
    to give AI agents full access to Beekeeper functionality.
    """
    load_project(name)

    host = request.host

    content = f"""# Beekeeper: {name}

**Use the `beekeeper` CLI for all operations.**

> **If anything in these instructions doesn't make sense or a command doesn't exist,
> this file may be stale. Refresh it with:**
> ```bash
> beekeeper projects instructions {name}
> ```
> Reload the output into your context before continuing.

Base URL: http://{host}

## Step 1 — Verify CLI is installed and current

```bash
beekeeper version
```

- If "command not found" — **stop and tell the user** the CLI needs to be installed. Do not install it yourself.
- If "UPDATE REQUIRED" — **stop and tell the user** their CLI is out of date and show them the update command from the output.
- If "UP TO DATE" — proceed to Step 2.

Installation command (for user to run if CLI is missing):

```bash
curl -L -o /usr/local/bin/beekeeper {CLI_RELEASE_BASE}/v{CLI_VERSION}/beekeeper && chmod +x /usr/local/bin/beekeeper
```

## Step 2 — Configure

```bash
export BEEKEEPER_HOST="http://{host}"
export BEEKEEPER_API_KEY="your-api-key"   # omit if auth is disabled
```

## Step 3 — Get current state

```bash
beekeeper training status {name}
beekeeper run analyze {name}
```

## Before Your First Analysis — Ask This Once

Before analyzing training for the first time, ask the user:

> "What is the primary metric I should treat as the performance signal for this project,
> and is higher or lower better? For example: `Train/episode_reward` (higher is better),
> or `val_loss` (lower is better)."

Remember the answer so you don't ask again. This determines what counts as a good run —
everything else explains *why* the primary metric looks the way it does.

---

## CLI Commands for This Project

```bash
beekeeper training status {name}         Current training status
beekeeper training start {name}          Start training
beekeeper training stop {name}           Stop training
beekeeper run analyze {name}             Synthesized metrics + trend analysis
beekeeper logs get {name} [tail]         Fetch log output (default: 100 lines)
beekeeper branch list {name}             List available branches
beekeeper branch switch {name} <branch>  Switch branch (training must be stopped)
beekeeper projects info {name}           Project config details
beekeeper stats                          System stats (GPU, CPU, RAM)
beekeeper busy                           Check if any training is running
```

## Workflows

### Analyze a running or completed training run
```bash
beekeeper run analyze {name}     # TensorBoard metrics + episode trends combined
beekeeper logs get {name} 100    # raw log output for errors or debug messages
```

`run analyze` uses both TensorBoard metrics and log-based episode analysis. If TensorBoard data isn't flushed yet, the log-based section still works.

### Start training
```bash
beekeeper busy                        # confirm nothing else is running
beekeeper training status {name}      # verify status is idle
beekeeper training start {name}
```

### Stop training
```bash
beekeeper training status {name}      # verify it's running
beekeeper training stop {name}
```

### Switch branches
```bash
beekeeper training status {name}      # must be idle before switching
beekeeper branch list {name}
beekeeper branch switch {name} <branch>
```

## Understanding `run analyze` Output

`beekeeper run analyze {name}` returns two sections: TensorBoard metrics and episode log analysis.

**Which run does it analyze?**
- If training is **running** — current active run
- If training is **idle** — most recent completed run

**Key fields in each metric:**

| Field | Meaning |
|-------|---------|
| `trend` | Overall trend: `improving`, `stable`, `worsening`, or `unstable` |
| `recent_trend` | Trend of the last 20% of steps — may differ from overall trend |
| `improvement_percent` | Change from start to end |
| `peak_value` | Best value reached at any point |
| `peak_step` | Step at which the peak occurred |
| `peak_reversal_pct` | How far the metric moved away from its peak, as % of total range |
| `converged` | Has the metric stabilized? |
| `convergence_step` | Step where convergence was detected |
| `anomaly_count` | Number of unusual spikes or drops |

**Interpreting trends:**
- `improving`: Moving in the expected direction (loss ↓, reward ↑)
- `stable`: Leveled off — convergence or plateau
- `worsening`: Confidently moving the wrong direction — needs attention
- `unstable`: High variance, no clear direction

## In-Depth Analysis Guide

When asked to analyze training performance, follow this approach regardless of project type:

**1. Identify the primary performance metric first.**
Look for metrics with names containing: `reward`, `score`, `return`, `accuracy`, `success_rate`, `win_rate`.
This is the headline. Everything else explains *why* it looks the way it does.

**2. Scan for critical signals before summarizing anything.**
These warrant immediate attention, in priority order:
- `trend: worsening` on any metric — actively degrading
- `peak_reversal_pct > 50` on a reward/performance metric — peaked and significantly reversed
- `recent_trend` differs from `trend` — the run changed direction in the final stretch
- `peak_step` very early relative to total steps — performance peaked early and never recovered
- High `anomaly_count` — spikes or instability

**3. Separate metric categories before drawing conclusions.**
- **Performance metrics** (`reward`, `score`, `accuracy`): answer "is the agent improving?"
- **Loss metrics** (`q_loss`, `td_error`, `reconstruction_loss`): answer "is the model learning correctly?"
- **Schedule metrics** (`epsilon`, `lr`, `temperature`): intentional decays — ignore as performance signals
- ⚠️ Improving losses with a declining reward signal is a warning: the model may be optimizing the wrong objective.

**4. Use `peak_reversal_pct` and `peak_step` to tell the full story.**
A metric labeled `improving` overall can still have degraded significantly from its peak:
- `peak_reversal_pct > 20%`: worth mentioning
- `peak_reversal_pct > 50%`: red flag — report peak value, peak step, and current value
- `peak_reversal_pct > 80%`: the run largely reversed — this is likely the primary finding

**5. Lead with the performance verdict, use losses to explain it.**
Don't open with "reconstruction loss improved 99%." Open with where the reward/score stands,
then use the losses to explain why — or why it degraded despite good losses.
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

@api_v1_bp.route("/projects/<name>/branches")
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
            timeout=30
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

    # Check if training is running
    status = get_training_status(name)
    if status["status"] == "running":
        return api_response(
            error_code="TRAINING_RUNNING",
            error_message="Cannot switch branches while training is running",
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
        # Check for uncommitted changes
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workspace_dir,
            capture_output=True,
            text=True,
            timeout=10
        )

        if status_result.stdout.strip():
            return api_response(
                error_code="UNCOMMITTED_CHANGES",
                error_message="Uncommitted changes in workspace",
                status_code=409
            )

        # Fetch from origin
        fetch_result = subprocess.run(
            ["git", "fetch", "origin"],
            cwd=workspace_dir,
            capture_output=True,
            text=True,
            timeout=60
        )

        if fetch_result.returncode != 0:
            return api_response(
                error_code="FETCH_FAILED",
                error_message=f"Failed to fetch: {fetch_result.stderr.strip()}",
                status_code=500
            )

        # Checkout the branch - try local first, then track remote
        checkout_result = subprocess.run(
            ["git", "checkout", new_branch],
            cwd=workspace_dir,
            capture_output=True,
            text=True,
            timeout=30
        )

        # If local checkout failed, try to track remote branch
        if checkout_result.returncode != 0:
            checkout_result = subprocess.run(
                ["git", "checkout", "--track", f"origin/{new_branch}"],
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


@api_v1_bp.route("/projects/<name>/agent/sdk")
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
