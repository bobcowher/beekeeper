import datetime

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from models.project import normalize_folder
from services import pipeline_engine, pipeline_store
from services.db_service import get_db
from services.folder_service import list_folders
from services.pipeline_schema import NAME_RE

pipelines_bp = Blueprint("pipelines", __name__, url_prefix="/pipelines")

# A new pipeline opens in the graphical editor; one runnable step shows what a step looks like.
_STARTER_STEP = {"name": "prepare", "type": "task",
                 "command": 'echo "Preparing data in $BEEKEEPER_PIPELINE_RUN_DIR"'}


def _ago(iso: str) -> str:
    secs = (datetime.datetime.now() - datetime.datetime.fromisoformat(iso)).total_seconds()
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{int(secs // size)}{unit} ago"
    return "just now"


@pipelines_bp.route("", methods=["GET"])
def index():
    pipelines = []
    for defn in pipeline_store.list_pipelines(current_app.config["PIPELINES_DIR"]):
        latest = get_db().latest_pipeline_run(defn["name"])
        pipelines.append({
            "defn": defn,
            "last": pipeline_engine.describe_run(latest) if latest else None,
            # Epoch seconds, like projects' last_run_at, so the shared list sort compares numbers.
            "last_run_at": datetime.datetime.fromisoformat(latest["started_at"]).timestamp() if latest else 0,
            "last_run_ago": _ago(latest["started_at"]) if latest else "",
        })
    return render_template("pipelines.html", pipelines=pipelines)


@pipelines_bp.route("/new", methods=["GET"])
def new():
    folders = list_folders(current_app.config["PROJECTS_DIR"], current_app.config["PIPELINES_DIR"])
    return render_template("new_pipeline.html", folders=folders)


@pipelines_bp.route("/new", methods=["POST"])
def create():
    pipelines_dir = current_app.config["PIPELINES_DIR"]
    name = request.form.get("name", "").strip()
    if not NAME_RE.match(name):
        flash("Pipeline names use lowercase letters, digits, '_' or '-'.", "error")
        return redirect(url_for("pipelines.new"))
    if pipeline_store.pipeline_exists(pipelines_dir, name):
        flash(f"Pipeline '{name}' already exists.", "error")
        return redirect(url_for("pipelines.new"))
    try:
        folder = normalize_folder(request.form.get("folder", ""))
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("pipelines.new"))
    pipeline_store.save_pipeline(pipelines_dir, {"name": name, "folder": folder, "steps": [_STARTER_STEP]})
    return redirect(url_for("pipelines.edit", name=name))


@pipelines_bp.route("/<name>", methods=["GET"])
def detail(name):
    if not pipeline_store.pipeline_exists(current_app.config["PIPELINES_DIR"], name):
        abort(404)
    return render_template("pipeline.html", name=name)


@pipelines_bp.route("/<name>/edit", methods=["GET"])
def edit(name):
    pipelines_dir = current_app.config["PIPELINES_DIR"]
    if not pipeline_store.pipeline_exists(pipelines_dir, name):
        abort(404)
    return render_template("edit_pipeline.html", name=name,
                           yaml=pipeline_store.read_pipeline_text(pipelines_dir, name),
                           folders=list_folders(current_app.config["PROJECTS_DIR"], pipelines_dir))
