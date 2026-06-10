import os
import json
from flask import Blueprint, render_template, current_app
from services.process_manager import get_runs_for_project
from services.db_service import get_db

dashboard_bp = Blueprint("dashboard", __name__)


def _format_runtime(seconds: int) -> str:
    if not seconds:
        return ""
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
                raw = get_db().get_project_total_runtime(name)
                p["total_runtime_seconds"] = raw
                p["total_runtime"] = _format_runtime(raw)
                projects.append(p)

    return render_template("dashboard.html", projects=projects)
