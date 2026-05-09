import os
import json
from flask import Blueprint, render_template, current_app
from services.process_manager import get_runs_for_project

dashboard_bp = Blueprint("dashboard", __name__)


def _effective_train_status(name: str, stored_status: str) -> str:
    """Return live status if any run is active; otherwise return stored status."""
    live_runs = get_runs_for_project(name)
    if any(r.get("status") in ("running", "starting") for r in live_runs):
        return "running"
    return stored_status


@dashboard_bp.route("/", methods=["GET"])
def index():
    projects_dir = current_app.config["PROJECTS_DIR"]
    projects = []

    if os.path.exists(projects_dir):
        for name in sorted(os.listdir(projects_dir)):
            config_path = os.path.join(projects_dir, name, "project.json")
            if os.path.isfile(config_path):
                with open(config_path) as f:
                    p = json.load(f)
                p["train_status"] = _effective_train_status(
                    name, p.get("train_status", "idle")
                )
                projects.append(p)

    return render_template("dashboard.html", projects=projects)
