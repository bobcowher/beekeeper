import io
import os
import zipfile
from flask import Blueprint, current_app, jsonify, request, send_file, abort

from services.db_service import get_db

runs_bp = Blueprint("runs", __name__, url_prefix="/projects")


def _fmt_size(size):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def _resolve_run_id(projects_dir, name, run_id_str):
    """Resolve 'latest' or a numeric string to an int run_id."""
    if run_id_str == "latest":
        runs = get_db().get_training_runs(name, limit=50)
        for run in runs:  # newest first
            rid = run["id"]
            if os.path.isdir(os.path.join(projects_dir, name, "persistent", "runs", f"run_{rid}")):
                return rid
        return None
    try:
        return int(run_id_str)
    except (ValueError, TypeError):
        return None


def _safe_run_path(projects_dir, name, run_id, subpath):
    """Resolve and validate a path inside persistent/runs/run_{run_id}/."""
    run_dir = os.path.realpath(
        os.path.join(projects_dir, name, "persistent", "runs", f"run_{run_id}")
    )
    target = os.path.realpath(os.path.join(run_dir, subpath)) if subpath else run_dir
    if not (target == run_dir or target.startswith(run_dir + os.sep)):
        return None, None
    return run_dir, target


def _zip_directory(dir_path, zip_name):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(dir_path):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
            for f in files:
                if f.startswith("."):
                    continue
                full = os.path.join(root, f)
                zf.write(full, os.path.relpath(full, dir_path))
    buf.seek(0)
    safe_name = zip_name.replace("/", "-").replace("\\", "-")
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{safe_name}.zip")


@runs_bp.route("/<name>/runs/")
def list_runs(name):
    """List all runs for a project with artifact availability."""
    projects_dir = current_app.config["PROJECTS_DIR"]
    if not os.path.isdir(os.path.join(projects_dir, name)):
        abort(404)

    runs = get_db().get_training_runs(name, limit=100)
    result = []
    for run in runs:
        rid = run["id"]
        persistent_dir = os.path.join(projects_dir, name, "persistent", "runs", f"run_{rid}")
        has_artifacts = os.path.isdir(persistent_dir) and bool(os.listdir(persistent_dir))
        result.append({
            "run_id": rid,
            "status": run.get("status"),
            "branch": run.get("branch"),
            "started_at": run.get("started_at"),
            "ended_at": run.get("ended_at"),
            "duration_seconds": run.get("duration_seconds"),
            "commit_sha": run.get("commit_sha"),
            "has_artifacts": has_artifacts,
            "files_url": f"/projects/{name}/runs/{rid}/files/",
        })

    return jsonify({
        "project": name,
        "runs": result,
        "latest_url": f"/projects/{name}/runs/latest/files/",
    })


@runs_bp.route("/<name>/runs/<run_id_str>/files/")
@runs_bp.route("/<name>/runs/<run_id_str>/files/<path:subpath>")
def run_files(name, run_id_str, subpath=""):
    """Browse or download files from a specific run's persistent storage.

    run_id_str can be an integer or 'latest'.
    """
    projects_dir = current_app.config["PROJECTS_DIR"]
    if not os.path.isdir(os.path.join(projects_dir, name)):
        abort(404)

    run_id = _resolve_run_id(projects_dir, name, run_id_str)
    if run_id is None:
        abort(404)

    _, target = _safe_run_path(projects_dir, name, run_id, subpath)
    if target is None:
        abort(403)

    if not os.path.exists(target):
        abort(404)

    if os.path.isfile(target):
        return send_file(target, as_attachment=True)

    if request.args.get("zip") == "1":
        label = subpath.replace("/", "-") if subpath else f"run_{run_id}"
        return _zip_directory(target, label)

    try:
        items = sorted(os.listdir(target))
    except PermissionError:
        abort(403)

    entries = []
    for item in items:
        if item.startswith(".") or item == "__pycache__":
            continue
        full = os.path.join(target, item)
        rel = os.path.join(subpath, item) if subpath else item
        mtime = os.path.getmtime(full)
        if os.path.isdir(full):
            entries.append({
                "name": item, "type": "dir", "path": rel,
                "size": None, "size_h": None, "mtime": mtime,
                "url": f"/projects/{name}/runs/{run_id}/files/{rel}/",
            })
        else:
            sz = os.path.getsize(full)
            entries.append({
                "name": item, "type": "file", "path": rel,
                "size": sz, "size_h": _fmt_size(sz), "mtime": mtime,
                "url": f"/projects/{name}/runs/{run_id}/files/{rel}",
            })

    entries.sort(key=lambda e: (0 if e["type"] == "dir" else 1, e["name"].lower()))

    host = request.host
    base = f"http://{host}/projects/{name}/runs/{run_id}/files"
    return jsonify({
        "project": name,
        "run_id": run_id,
        "path": subpath or "",
        "entries": entries,
        "curl_examples": {
            "download_file": f"curl -O {base}/<filepath>",
            "download_dir_zip": f"curl -o run_{run_id}.zip '{base}/?zip=1'",
        },
    })
