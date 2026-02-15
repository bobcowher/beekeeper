import os
import re
import json
from flask import (
    Blueprint, render_template, current_app,
    request, redirect, url_for, abort, flash,
)

from services.project_service import create_project, delete_project
from services.python_versions import find_available

project_bp = Blueprint("project", __name__, url_prefix="/projects")


@project_bp.route("/new")
def new():
    python_versions = find_available()
    return render_template("create_project.html", python_versions=python_versions)


@project_bp.route("/create", methods=["POST"])
def create():
    projects_dir = current_app.config["PROJECTS_DIR"]
    name = request.form.get("name", "").strip()

    # Validate name: alphanumeric, hyphens, underscores only
    if not name or not re.match(r"^[a-zA-Z0-9_-]+$", name):
        flash("Invalid project name. Use only letters, numbers, hyphens, underscores.", "error")
        return redirect(url_for("project.new"))

    # Check for duplicate
    if os.path.exists(os.path.join(projects_dir, name)):
        flash(f"Project '{name}' already exists.", "error")
        return redirect(url_for("project.new"))

    git_url = request.form.get("git_url", "").strip()
    if not git_url:
        flash("Git URL is required.", "error")
        return redirect(url_for("project.new"))

    data = {
        "name": name,
        "git_url": git_url,
        "branch": request.form.get("branch", "main").strip() or "main",
        "python_version": request.form.get("python_version", "3.12"),
        "train_file": request.form.get("train_file", "train.py").strip() or "train.py",
        "tensorboard_log_dir": request.form.get("tensorboard_log_dir", "runs").strip() or "runs",
        "requirements_file": request.form.get("requirements_file", "requirements.txt").strip() or "requirements.txt",
    }

    create_project(projects_dir, data)
    return redirect(url_for("project.detail", name=name))


@project_bp.route("/<name>")
def detail(name):
    config_path = os.path.join(
        current_app.config["PROJECTS_DIR"], name, "project.json"
    )
    if not os.path.isfile(config_path):
        abort(404)

    with open(config_path) as f:
        project = json.load(f)

    return render_template("project.html", project=project)


@project_bp.route("/<name>/delete", methods=["POST"])
def delete(name):
    projects_dir = current_app.config["PROJECTS_DIR"]
    config_path = os.path.join(projects_dir, name, "project.json")
    if not os.path.isfile(config_path):
        abort(404)

    delete_project(projects_dir, name)
    return redirect(url_for("dashboard.index"))
