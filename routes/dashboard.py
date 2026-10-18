import os

from flask import Blueprint, current_app, jsonify, render_template

from services import pipeline_engine, run_activity
from services.db_service import get_db
from services.process_manager import get_runs_for_project

dashboard_bp = Blueprint("dashboard", __name__)


def _running_training(projects_dir):
    running = []
    if not os.path.isdir(projects_dir):
        return running
    for name in sorted(os.listdir(projects_dir)):
        if not os.path.isfile(os.path.join(projects_dir, name, "project.json")):
            continue
        for run in get_runs_for_project(name):
            row = get_db().get_run(name, run["run_id"])
            running.append({
                "kind": "training", "name": name, "run_id": run["run_id"],
                "run_ref": run["run_ref"], "branch": run.get("branch") or "",
                "elapsed": run.get("elapsed"), "status": run.get("status", "running"),
                "where": run_activity.where(row), "cost": run_activity.cost(row),
                "url": f"/projects/{name}",
            })
    return running


def _running_pipelines(db):
    """Running pipeline runs. One that's only waiting in a project queue is left out:
    the Queued list shows it, through its queue entry."""
    running = []
    for row in db.running_pipeline_runs():
        run = pipeline_engine.describe_run(row)
        if run["waiting"]:
            continue
        index = run.get("current_step", 0)
        step = run["steps"][index] if index < len(run["steps"]) else None
        steps, _, cost = run_activity.pipeline_steps(db, row, finished=False)
        running.append({
            "kind": "pipeline", "name": run["pipeline"], "run_id": run["run_id"],
            "run_ref": run["run_ref"], "current_step": step["name"] if step else "",
            "elapsed": run_activity.seconds_between(run.get("started_at")), "status": "running",
            "steps": steps, "cost": cost,
            "url": f"/pipelines/{run['pipeline']}",
        })
    return running


def _queued_item(entry):
    source = entry.get("source") or ""
    pipeline_ref = source[len("pipeline:"):] if source.startswith("pipeline:") else None
    return {
        "project": entry["project_name"], "branch": entry.get("branch") or "",
        "position": entry["position"], "url": f"/projects/{entry['project_name']}",
        "pipeline_ref": pipeline_ref,
        "pipeline_url": f"/pipelines/{pipeline_ref.rsplit('#', 1)[0]}" if pipeline_ref else None,
        # The pipeline's own handoff variables say nothing about the experiment.
        "env": {k: v for k, v in (entry.get("env_vars") or {}).items() if not k.startswith("BEEKEEPER_")},
    }


def _activity_data():
    db = get_db()
    projects_dir = current_app.config["PROJECTS_DIR"]
    running = _running_training(projects_dir) + _running_pipelines(db)
    queued = [_queued_item(entry) for entry in db.list_all_queued_runs()]
    return {"running": running, "queued": queued}


@dashboard_bp.route("/", methods=["GET"])
def index():
    return render_template("dashboard.html", activity=_activity_data())


@dashboard_bp.route("/dashboard/activity", methods=["GET"])
def activity():
    return jsonify(_activity_data())
