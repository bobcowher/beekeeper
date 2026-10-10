from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from models.project import normalize_folder
from services import pipeline_store
from services.folder_service import list_folders
from services.pipeline_schema import NAME_RE

pipelines_bp = Blueprint("pipelines", __name__, url_prefix="/pipelines")

_TEMPLATE_STEP = {"name": "hello", "type": "task", "command": 'echo "Pipeline run dir: $BEEKEEPER_PIPELINE_RUN_DIR"'}


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
    pipeline_store.save_pipeline(pipelines_dir, {"name": name, "folder": folder, "steps": [dict(_TEMPLATE_STEP)]})
    return redirect(url_for("pipelines.detail", name=name))


@pipelines_bp.route("/<name>", methods=["GET"])
def detail(name):
    if not pipeline_store.pipeline_exists(current_app.config["PIPELINES_DIR"], name):
        abort(404)
    return render_template("pipeline.html", name=name)
