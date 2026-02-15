import os
import json
from flask import Blueprint, render_template, current_app

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
def index():
    projects_dir = current_app.config["PROJECTS_DIR"]
    projects = []

    if os.path.exists(projects_dir):
        for name in sorted(os.listdir(projects_dir)):
            config_path = os.path.join(projects_dir, name, "project.json")
            if os.path.isfile(config_path):
                with open(config_path) as f:
                    projects.append(json.load(f))

    return render_template("dashboard.html", projects=projects)
