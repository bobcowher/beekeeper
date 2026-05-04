import os
import time
from flask import Blueprint, current_app, jsonify, request, Response, send_file

from services.process_manager import (
    start_training, stop_training, get_training_status,
    start_tensorboard, stop_tensorboard, get_run_log_path, get_runs_for_project,
)

training_bp = Blueprint("training", __name__, url_prefix="/projects")


@training_bp.route("/<name>/start", methods=["POST"])
def start(name):
    projects_dir = current_app.config["PROJECTS_DIR"]
    config_path = os.path.join(projects_dir, name, "project.json")
    if not os.path.isfile(config_path):
        return jsonify({"error": "Project not found"}), 404

    result = start_training(projects_dir, name)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)


@training_bp.route("/<name>/stop", methods=["POST"])
def stop(name):
    projects_dir = current_app.config["PROJECTS_DIR"]
    result = stop_training(projects_dir, name)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)


@training_bp.route("/<name>/status")
def status(name):
    return jsonify(get_training_status(name))


@training_bp.route("/<name>/tensorboard/start", methods=["POST"])
def tb_start(name):
    projects_dir = current_app.config["PROJECTS_DIR"]
    result = start_tensorboard(projects_dir, name)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)


@training_bp.route("/<name>/tensorboard/stop", methods=["POST"])
def tb_stop(name):
    result = stop_tensorboard(name)
    return jsonify(result)


def _tail_offset(filepath, lines):
    """Find the byte offset to start reading the last N lines of a file."""
    try:
        size = os.path.getsize(filepath)
    except OSError:
        return 0
    if size == 0:
        return 0

    buf_size = 8192
    found = 0
    offset = size

    with open(filepath, "rb") as f:
        while offset > 0 and found <= lines:
            read_size = min(buf_size, offset)
            offset -= read_size
            f.seek(offset)
            chunk = f.read(read_size)
            found += chunk.count(b"\n")

        # If we found enough lines, seek forward to the right start
        if found > lines:
            f.seek(offset)
            data = f.read()
            idx = 0
            skip = found - lines
            for _ in range(skip):
                idx = data.index(b"\n", idx) + 1
            return offset + idx

    return offset


@training_bp.route("/<name>/logs/stream")
def logs_stream(name):
    projects_dir = current_app.config["PROJECTS_DIR"]
    tail = request.args.get("tail", type=int)
    run_id = request.args.get("run_id", type=int)
    log_path = get_run_log_path(projects_dir, name, run_id=run_id)

    def generate():
        if tail and os.path.isfile(log_path):
            offset = _tail_offset(log_path, tail)
        else:
            offset = 0

        retries_without_data = 0
        max_idle = 300

        while True:
            try:
                if os.path.isfile(log_path):
                    size = os.path.getsize(log_path)
                    if size < offset:
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
                runs = get_runs_for_project(name)
                if run_id is not None:
                    run_active = any(r["run_id"] == run_id for r in runs)
                else:
                    run_active = len(runs) > 0
                if not run_active and retries_without_data > 2:
                    yield "data: \n\nevent: done\ndata: finished\n\n"
                    return
                if retries_without_data > max_idle:
                    return

            except Exception:
                pass

            time.sleep(0.5)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@training_bp.route("/<name>/logs/download")
def logs_download(name):
    projects_dir = current_app.config["PROJECTS_DIR"]
    run_id = request.args.get("run_id", type=int)
    log_path = get_run_log_path(projects_dir, name, run_id=run_id)
    if not os.path.isfile(log_path):
        return jsonify({"error": "No log file found"}), 404
    filename = f"{name}-run-{run_id}.log" if run_id else f"{name}-train.log"
    return send_file(log_path, as_attachment=True, download_name=filename)


@training_bp.route("/<name>/history")
def history(name):
    """Get training run history for a project."""
    from services.db_service import get_db
    projects_dir = current_app.config["PROJECTS_DIR"]

    config_path = os.path.join(projects_dir, name, "project.json")
    if not os.path.isfile(config_path):
        return jsonify({"error": "Project not found"}), 404

    runs = get_db().get_training_runs(name, limit=20)
    return jsonify({"runs": runs})


@training_bp.route("/<name>/history/<int:run_id>/log")
def history_log(name, run_id):
    """Download archived log for a specific run."""
    from services.db_service import get_db
    projects_dir = current_app.config["PROJECTS_DIR"]

    run = get_db().get_training_run(run_id)
    if not run or run['project_name'] != name:
        return jsonify({"error": "Run not found"}), 404

    if not run.get('log_file_path'):
        return jsonify({"error": "Log file not available"}), 404

    log_path = os.path.join(projects_dir, name, run['log_file_path'])
    if not os.path.isfile(log_path):
        return jsonify({"error": "Log file not found"}), 404

    return send_file(log_path, as_attachment=True,
                     download_name=f"{name}-run-{run_id}.log")


@training_bp.route("/<name>/history/diff")
def history_diff(name):
    """Git diff between two runs' commits."""
    import subprocess
    from services.db_service import get_db

    from_id = request.args.get('from', type=int)
    to_id = request.args.get('to', type=int)
    if not from_id or not to_id:
        return jsonify({"error": "from and to run IDs required"}), 400

    from_run = get_db().get_training_run(from_id)
    to_run = get_db().get_training_run(to_id)
    if not from_run or from_run['project_name'] != name:
        return jsonify({"error": "Run not found"}), 404
    if not to_run or to_run['project_name'] != name:
        return jsonify({"error": "Run not found"}), 404

    from_sha = from_run.get('commit_sha')
    to_sha = to_run.get('commit_sha')
    if not from_sha or not to_sha:
        return jsonify({"error": "Commit SHA not available for one or both runs"}), 422

    if from_sha == to_sha:
        return jsonify({"diff": "", "same_commit": True,
                        "from_sha": from_sha[:7], "to_sha": to_sha[:7]})

    projects_dir = current_app.config["PROJECTS_DIR"]
    workspace_dir = os.path.join(projects_dir, name, "workspace")
    if not os.path.isdir(workspace_dir):
        return jsonify({"error": "Workspace not found"}), 422

    try:
        result = subprocess.run(
            ["git", "diff", from_sha, to_sha],
            cwd=workspace_dir, capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return jsonify({"error": f"git diff failed: {result.stderr.strip()}"}), 422
        return jsonify({
            "diff": result.stdout,
            "same_commit": False,
            "from_sha": from_sha[:7],
            "to_sha": to_sha[:7]
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "git diff timed out"}), 422
    except Exception as e:
        return jsonify({"error": str(e)}), 422


@training_bp.route("/<name>/history/<int:run_id>", methods=["PATCH"])
def update_run(name, run_id):
    """Update annotations (notes, notable, tags) on a run."""
    from services.db_service import get_db

    run = get_db().get_training_run(run_id)
    if not run or run['project_name'] != name:
        return jsonify({"error": "Run not found"}), 404

    data = request.get_json() or {}
    kwargs = {}
    if 'notes' in data:
        kwargs['notes'] = str(data['notes'])
    if 'notable' in data:
        kwargs['notable'] = 1 if data['notable'] else 0
    if 'tags' in data:
        kwargs['tags'] = str(data['tags'])

    if kwargs:
        get_db().update_run_annotations(run_id, **kwargs)

    return jsonify({"ok": True})


@training_bp.route("/<name>/history/clear", methods=["POST"])
def clear_history(name):
    """Clear all run history for a project."""
    from services.db_service import get_db
    import shutil
    projects_dir = current_app.config["PROJECTS_DIR"]

    config_path = os.path.join(projects_dir, name, "project.json")
    if not os.path.isfile(config_path):
        return jsonify({"error": "Project not found"}), 404

    # Get all runs to delete files
    runs = get_db().get_training_runs(name, limit=None)

    # Delete archived logs and Tensorboard directories
    for run in runs:
        if run.get('log_file_path'):
            log_path = os.path.join(projects_dir, name, run['log_file_path'])
            if os.path.isfile(log_path):
                try:
                    os.unlink(log_path)
                except Exception:
                    pass

        if run.get('tensorboard_dir'):
            tb_path = os.path.join(projects_dir, name, "workspace", run['tensorboard_dir'])
            if os.path.isdir(tb_path):
                try:
                    shutil.rmtree(tb_path)
                except Exception:
                    pass

    # Delete run_logs directory if empty
    run_logs_dir = os.path.join(projects_dir, name, "run_logs")
    if os.path.isdir(run_logs_dir):
        try:
            os.rmdir(run_logs_dir)
        except OSError:
            pass  # Directory not empty, leave it

    # Delete all database records
    count = get_db().delete_all_runs(name)

    return jsonify({"message": f"Deleted {count} runs"})
