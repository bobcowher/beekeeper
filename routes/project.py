import os
import re
import json
from flask import (
    Blueprint, render_template, current_app,
    request, redirect, url_for, abort, flash, jsonify,
)

from models.project import Project
from services.project_service import (
    create_project,
    delete_project,
    retry_setup,
    validate_output_paths,
)
from services.python_versions import find_available, has_conda
from services.process_manager import get_training_status, stop_tensorboard, get_runs_for_project
from services.run_storage_service import delete_run_storage
from services.db_service import get_db
from services.ssh_key_service import is_ssh_auth_error

project_bp = Blueprint("project", __name__, url_prefix="/projects")

_PROJECT_FILE = "project.json"
_DETAIL_ROUTE = "project.detail"
_NEW_ROUTE = "project.new"
_EDIT_ROUTE = "project.edit"


def _format_runtime(seconds: int) -> str:
    """Human-readable duration: '3d 2h 15m', '45m', '< 1m'."""
    if not seconds:
        return "—"
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    return " ".join(parts) if parts else "< 1m"


@project_bp.route("/new", methods=["GET"])
def new():
    python_versions = find_available()
    conda_available = has_conda()
    return render_template(
        "create_project.html",
        python_versions=python_versions,
        conda_available=conda_available,
    )


@project_bp.route("/create", methods=["POST"])
def create():
    projects_dir = current_app.config["PROJECTS_DIR"]
    name = request.form.get("name", "").strip()

    # Validate name: alphanumeric, hyphens, underscores only
    if not name or not re.match(r"^[a-zA-Z0-9_-]+$", name):
        flash("Invalid project name. Use only letters, numbers, hyphens, underscores.", "error")
        return redirect(url_for(_NEW_ROUTE))

    # Check for duplicate
    if os.path.exists(os.path.join(projects_dir, name)):
        flash(f"Project '{name}' already exists.", "error")
        return redirect(url_for(_NEW_ROUTE))

    git_url = request.form.get("git_url", "").strip()
    if not git_url:
        flash("Git URL is required.", "error")
        return redirect(url_for(_NEW_ROUTE))

    data_dir_enabled = request.form.get("data_dir_enabled") == "1"
    data_dir_local = request.form.get("data_dir_local", "data").strip() or "data"
    data_dir_remote = request.form.get("data_dir_remote", "").strip()

    if data_dir_enabled:
        if not data_dir_remote:
            flash("System data path is required when data directory is enabled.", "error")
            return redirect(url_for(_NEW_ROUTE))
        if not os.path.isdir(data_dir_remote):
            flash(f"System data path '{data_dir_remote}' does not exist or is not a directory.", "error")
            return redirect(url_for(_NEW_ROUTE))

    tensorboard_log_dir = request.form.get("tensorboard_log_dir", "runs").strip() or "runs"
    try:
        output_paths = validate_output_paths(
            request.form.get("output_paths", ""),
            tensorboard_log_dir,
        )
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for(_NEW_ROUTE))

    env_keys = request.form.getlist("env_key")
    env_vals = request.form.getlist("env_val")
    env_vars = {}
    for k, v in zip(env_keys, env_vals):
        k = k.strip()
        if k:
            env_vars[k] = v

    data = {
        "name": name,
        "git_url": git_url,
        "branch": request.form.get("branch", "main").strip() or "main",
        "python_version": request.form.get("python_version", "3.12"),
        "train_file": request.form.get("train_file", "train.py").strip() or "train.py",
        "tensorboard_log_dir": tensorboard_log_dir,
        "requirements_file": request.form.get("requirements_file", "requirements.txt").strip() or "requirements.txt",
        "env_type": request.form.get("env_type", "venv"),
        "setup_script": request.form.get("setup_script", "").strip(),
        "data_dir_enabled": data_dir_enabled,
        "data_dir_local": data_dir_local,
        "data_dir_remote": data_dir_remote,
        "output_paths": output_paths,
        "env_vars": env_vars,
    }

    create_project(projects_dir, data)
    return redirect(url_for(_DETAIL_ROUTE, name=name))


@project_bp.route("/<name>", methods=["GET"])
def detail(name):
    config_path = os.path.join(
        current_app.config["PROJECTS_DIR"], name, _PROJECT_FILE
    )  # NOSONAR
    if not os.path.isfile(config_path):
        abort(404)

    with open(config_path) as f:
        project = json.load(f)

    training = get_training_status(name)
    runs = get_runs_for_project(name)

    # Reconcile stale state: server restarted, _running cleared, but JSON still says 'running'
    if training['status'] == 'idle' and project.get('train_status') == 'running':
        projects_dir = current_app.config["PROJECTS_DIR"]
        p = Project.load(config_path)
        if p:
            p.train_status = 'stopped'
            p.save(projects_dir)
        project['train_status'] = 'stopped'

    total_runtime_seconds = get_db().get_project_total_runtime(name)
    beekeeper_home = current_app.config["BEEKEEPER_HOME"]
    mcp_server_path = os.path.join(beekeeper_home, "mcp_server.py")
    return render_template(
        "project.html",
        project=project,
        training=training,
        runs=runs,
        mcp_server_path=mcp_server_path,
        total_runtime=_format_runtime(total_runtime_seconds),
        total_runtime_seconds=total_runtime_seconds,
        ssh_auth_error=is_ssh_auth_error(project.get("setup_error", "")),
    )


@project_bp.route("/<name>/edit", methods=["GET"])
def edit(name):
    projects_dir = current_app.config["PROJECTS_DIR"]
    config_path = os.path.join(projects_dir, name, _PROJECT_FILE)  # NOSONAR
    if not os.path.isfile(config_path):
        abort(404)

    with open(config_path) as f:
        project = json.load(f)

    return render_template("edit_project.html", project=project)


@project_bp.route("/<name>/edit", methods=["POST"])
def update(name):
    projects_dir = current_app.config["PROJECTS_DIR"]
    config_path = os.path.join(projects_dir, name, _PROJECT_FILE)  # NOSONAR
    if not os.path.isfile(config_path):
        abort(404)

    with open(config_path) as f:
        project_data = json.load(f)

    # Update editable fields
    project_data["branch"] = request.form.get("branch", project_data["branch"]).strip()
    project_data["train_file"] = request.form.get("train_file", project_data["train_file"]).strip()
    project_data["tensorboard_log_dir"] = request.form.get("tensorboard_log_dir", project_data["tensorboard_log_dir"]).strip() or "runs"
    project_data["requirements_file"] = request.form.get("requirements_file", project_data["requirements_file"]).strip()
    project_data["setup_script"] = request.form.get("setup_script", project_data.get("setup_script", "")).strip()
    project_data["tb_logs_max_runs"] = request.form.get("tb_logs_max_runs", type=int) or project_data.get("tb_logs_max_runs", 10)
    project_data["run_history_max_runs"] = request.form.get("run_history_max_runs", type=int) or project_data.get("run_history_max_runs", 10)
    data_dir_enabled = request.form.get("data_dir_enabled") == "1"
    data_dir_local = request.form.get("data_dir_local", project_data.get("data_dir_local", "data")).strip() or "data"
    data_dir_remote = request.form.get("data_dir_remote", project_data.get("data_dir_remote", "")).strip()

    if data_dir_enabled:
        if not data_dir_remote:
            flash("System data path is required when data directory is enabled.", "error")
            return redirect(url_for(_EDIT_ROUTE, name=name))
        if not os.path.isdir(data_dir_remote):
            flash(f"System data path '{data_dir_remote}' does not exist or is not a directory.", "error")
            return redirect(url_for(_EDIT_ROUTE, name=name))
        workspace_dir = os.path.join(projects_dir, name, "workspace")
        if os.path.isdir(workspace_dir):
            local_path = os.path.join(workspace_dir, data_dir_local)
            if os.path.islink(local_path):
                if os.readlink(local_path) != data_dir_remote:
                    os.unlink(local_path)
                    os.symlink(data_dir_remote, local_path)
            elif os.path.exists(local_path):
                flash(
                    f"'{data_dir_local}' already exists in the repository and is not a symlink. "
                    f"Remove it first, then save again.",
                    "error",
                )
                return redirect(url_for(_EDIT_ROUTE, name=name))
            else:
                os.symlink(data_dir_remote, local_path)

    project_data["data_dir_enabled"] = data_dir_enabled
    project_data["data_dir_local"] = data_dir_local
    project_data["data_dir_remote"] = data_dir_remote

    try:
        project_data["output_paths"] = validate_output_paths(
            request.form.get("output_paths", ""),
            project_data["tensorboard_log_dir"],
        )
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for(_EDIT_ROUTE, name=name))

    # Parse environment variables from the form
    env_keys = request.form.getlist("env_key")
    env_vals = request.form.getlist("env_val")
    env_vars = {}
    for k, v in zip(env_keys, env_vals):
        k = k.strip()
        if k:
            env_vars[k] = v
    project_data["env_vars"] = env_vars

    # Handle parallel runs settings
    project_data["parallel_runs_enabled"] = bool(request.form.get("parallel_runs_enabled"))
    try:
        project_data["max_parallel_runs"] = max(2, int(request.form.get("max_parallel_runs", 2)))
    except (ValueError, TypeError):
        project_data["max_parallel_runs"] = 2

    # GPU memory management
    project_data["gpu_enabled"] = bool(request.form.get("gpu_enabled"))
    try:
        project_data["gpu_memory_minimum"] = max(0, int(request.form.get("gpu_memory_minimum") or 0))
    except (ValueError, TypeError):
        project_data["gpu_memory_minimum"] = 0
    try:
        project_data["gpu_memory_preferred"] = max(0, int(request.form.get("gpu_memory_preferred") or 0))
    except (ValueError, TypeError):
        project_data["gpu_memory_preferred"] = 0

    from models.project import Project
    project = Project(**project_data)
    project.save(projects_dir)

    flash("Project settings updated.", "success")
    return redirect(url_for(_DETAIL_ROUTE, name=name))


_SETUP_ACTIVE_STATUSES = {"pending", "cloning", "creating_env", "running_setup_script", "installing_deps"}


@project_bp.route("/<name>/rename", methods=["POST"])
def rename(name):
    projects_dir = current_app.config["PROJECTS_DIR"]
    config_path = os.path.join(projects_dir, name, _PROJECT_FILE)  # NOSONAR
    if not os.path.isfile(config_path):
        abort(404)

    with open(config_path) as f:
        project_data = json.load(f)

    if project_data.get("setup_status") in _SETUP_ACTIVE_STATUSES:
        flash("Cannot rename while setup is in progress.", "error")
        return redirect(url_for(_EDIT_ROUTE, name=name))

    training = get_training_status(name)
    if training["status"] != "idle":
        flash("Cannot rename while training is active.", "error")
        return redirect(url_for(_EDIT_ROUTE, name=name))

    new_name = request.form.get("new_name", "").strip()
    if not new_name or not re.match(r"^[a-zA-Z0-9_-]+$", new_name):
        flash("Invalid project name. Use only letters, numbers, hyphens, underscores.", "error")
        return redirect(url_for(_EDIT_ROUTE, name=name))

    if new_name == name:
        return redirect(url_for(_EDIT_ROUTE, name=name))

    new_dir = os.path.join(projects_dir, new_name)
    if os.path.exists(new_dir):
        flash(f"A project named '{new_name}' already exists.", "error")
        return redirect(url_for(_EDIT_ROUTE, name=name))

    old_dir = os.path.join(projects_dir, name)
    os.rename(old_dir, new_dir)

    project_data["name"] = new_name
    new_config_path = os.path.join(new_dir, _PROJECT_FILE)
    with open(new_config_path, "w") as f:
        json.dump(project_data, f, indent=2)

    from services.db_service import get_db
    get_db().rename_project_runs(name, new_name)

    flash(f"Project renamed to '{new_name}'.", "success")
    return redirect(url_for(_EDIT_ROUTE, name=new_name))


@project_bp.route("/<name>/retry-setup", methods=["POST"])
def retry(name):
    projects_dir = current_app.config["PROJECTS_DIR"]
    config_path = os.path.join(projects_dir, name, _PROJECT_FILE)  # NOSONAR
    if not os.path.isfile(config_path):
        abort(404)

    retry_setup(projects_dir, name)
    return redirect(url_for(_DETAIL_ROUTE, name=name))


@project_bp.route("/<name>/clear-tb-logs", methods=["POST"])
def clear_tb_logs(name):
    import shutil
    projects_dir = current_app.config["PROJECTS_DIR"]
    config_path = os.path.join(projects_dir, name, _PROJECT_FILE)  # NOSONAR
    if not os.path.isfile(config_path):
        abort(404)

    with open(config_path) as f:
        project = json.load(f)

    tb_logdir = os.path.join(projects_dir, name, "workspace", project.get("tensorboard_log_dir", "runs"))
    # tb_logdir may be a symlink into persistent/runs/run_<id>/ (set up by
    # _ensure_workspace_symlink for the most recent run) — shutil.rmtree refuses
    # to operate on a symlink itself, so clear the link target instead.
    clear_target = os.path.realpath(tb_logdir) if os.path.islink(tb_logdir) else tb_logdir
    if os.path.isdir(clear_target):
        shutil.rmtree(clear_target)
        os.makedirs(clear_target, exist_ok=True)
        flash("Tensorboard logs cleared.", "success")
    else:
        flash("Tensorboard log directory not found.", "error")

    return redirect(url_for(_DETAIL_ROUTE, name=name))


@project_bp.route("/<name>/cleanup-tb-logs", methods=["POST"])
def cleanup_tb_logs(name):
    from services.tensorboard_service import cleanup_old_tb_logs
    projects_dir = current_app.config["PROJECTS_DIR"]
    config_path = os.path.join(projects_dir, name, _PROJECT_FILE)  # NOSONAR
    if not os.path.isfile(config_path):
        abort(404)

    with open(config_path) as f:
        project = json.load(f)

    keep_count = request.form.get("keep_count", type=int)
    if not keep_count or keep_count < 1:
        flash("Please specify how many runs to keep (must be at least 1).", "error")
        return redirect(url_for(_DETAIL_ROUTE, name=name))

    tb_logdir = os.path.join(projects_dir, name, "workspace", project.get("tensorboard_log_dir", "runs"))
    result = cleanup_old_tb_logs(tb_logdir, keep_count)

    if result['deleted']:
        flash(result['message'], "success")
    else:
        flash(result['message'], "info")

    return redirect(url_for(_DETAIL_ROUTE, name=name))


@project_bp.route("/<name>/cleanup-run-history", methods=["POST"])
def cleanup_run_history(name):
    from services.db_service import get_db
    projects_dir = current_app.config["PROJECTS_DIR"]
    config_path = os.path.join(projects_dir, name, _PROJECT_FILE)  # NOSONAR
    if not os.path.isfile(config_path):
        abort(404)

    keep_count = request.form.get("keep_count", type=int)
    if not keep_count or keep_count < 1:
        flash("Please specify how many runs to keep (must be at least 1).", "error")
        return redirect(url_for(_DETAIL_ROUTE, name=name))

    db = get_db()
    runs = db.get_training_runs(name, limit=1000)

    # Sort by started_at descending (newest first)
    runs.sort(key=lambda r: r['started_at'], reverse=True)

    # Delete runs beyond keep_count
    deleted_count = 0
    for run in runs[keep_count:]:
        delete_run_storage(projects_dir, name, run)
        db.delete_training_run(run['id'])
        deleted_count += 1

    if deleted_count > 0:
        flash(f"Deleted {deleted_count} old run record(s), kept {min(len(runs), keep_count)} recent run(s).", "success")
    else:
        flash(f"Only {len(runs)} run(s) found, nothing to delete.", "info")

    return redirect(url_for(_DETAIL_ROUTE, name=name))


@project_bp.route("/<name>/pin", methods=["POST"])
def toggle_pin(name):
    projects_dir = current_app.config["PROJECTS_DIR"]
    config_path = os.path.join(projects_dir, name, _PROJECT_FILE)  # NOSONAR
    if not os.path.isfile(config_path):
        abort(404)
    p = Project.load(config_path)
    p.pinned = not p.pinned
    p.save(projects_dir)
    return jsonify({"pinned": p.pinned})


@project_bp.route("/<name>/delete", methods=["POST"])
def delete(name):
    projects_dir = current_app.config["PROJECTS_DIR"]
    config_path = os.path.join(projects_dir, name, _PROJECT_FILE)  # NOSONAR
    if not os.path.isfile(config_path):
        abort(404)

    stop_tensorboard(name)
    delete_project(projects_dir, name)
    return redirect(url_for("dashboard.index"))
