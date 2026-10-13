import datetime
import os

from flask import Blueprint, current_app, jsonify, render_template

from services import pipeline_engine
from services.db_service import get_db
from services.process_manager import get_runs_for_project

dashboard_bp = Blueprint("dashboard", __name__)


def _seconds_between(start, end=None):
    if not start:
        return None
    started = datetime.datetime.fromisoformat(start)
    ended = datetime.datetime.fromisoformat(end) if end else datetime.datetime.now()
    return max(0, int((ended - started).total_seconds()))


def _running_training(projects_dir):
    running = []
    if not os.path.isdir(projects_dir):
        return running
    for name in sorted(os.listdir(projects_dir)):
        if not os.path.isfile(os.path.join(projects_dir, name, "project.json")):
            continue
        for run in get_runs_for_project(name):
            running.append({
                "kind": "training", "name": name, "run_id": run["run_id"],
                "run_ref": run["run_ref"], "branch": run.get("branch") or "",
                "elapsed": run.get("elapsed"), "status": run.get("status", "running"),
                "url": f"/projects/{name}",
            })
    return running


def _running_pipelines(db):
    """Running pipeline runs, and the names of those only waiting in a project queue
    (the Queued list shows those, through their queue entry)."""
    running, waiting = [], set()
    for row in db.running_pipeline_runs():
        run = pipeline_engine.describe_run(row)
        if run["waiting"]:
            waiting.add(run["pipeline"])
            continue
        index = run.get("current_step", 0)
        step = run["steps"][index] if index < len(run["steps"]) else None
        running.append({
            "kind": "pipeline", "name": run["pipeline"], "run_id": run["run_id"],
            "run_ref": run["run_ref"], "current_step": step["name"] if step else "",
            "elapsed": _seconds_between(run.get("started_at")), "status": "running",
            "url": f"/pipelines/{run['pipeline']}",
        })
    return running, waiting


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


def _recent_activity(db, active):
    recent = []
    for run in db.recent_finished_training_runs(limit=20):
        if ("training", run["project_name"]) not in active:
            recent.append({
                "kind": "training", "name": run["project_name"], "run_id": run["run_number"],
                "run_ref": f"{run['project_name']}-{run['run_number']}", "status": run["status"],
                "duration": run.get("duration_seconds"),
                "timestamp": run.get("ended_at") or run["started_at"],
                "url": f"/projects/{run['project_name']}/history/{run['run_number']}/log",
            })
    for run in db.recent_finished_pipeline_runs(limit=20):
        if ("pipeline", run["pipeline_name"]) not in active:
            recent.append({
                "kind": "pipeline", "name": run["pipeline_name"], "run_id": run["run_number"],
                "run_ref": f"{run['pipeline_name']}#{run['run_number']}",
                "status": "completed" if run["status"] == "succeeded" else run["status"],
                "duration": _seconds_between(run["started_at"], run.get("ended_at")),
                "timestamp": run.get("ended_at") or run["started_at"],
                "url": f"/pipelines/{run['pipeline_name']}",
            })
    recent.sort(key=lambda item: item["timestamp"] or "", reverse=True)
    return recent[:20]


def _activity_data():
    db = get_db()
    projects_dir = current_app.config["PROJECTS_DIR"]
    pipelines, waiting = _running_pipelines(db)
    running = _running_training(projects_dir) + pipelines
    queued = [_queued_item(entry) for entry in db.list_all_queued_runs()]

    # One entry per project or pipeline: its latest finished run, unless it's active now
    # (then the Running or Queued list already shows it).
    active = {(item["kind"], item["name"]) for item in running}
    active |= {("pipeline", name) for name in waiting}
    return {"running": running, "queued": queued, "recent": _recent_activity(db, active)}


@dashboard_bp.route("/", methods=["GET"])
def index():
    return render_template("dashboard.html", activity=_activity_data())


@dashboard_bp.route("/dashboard/activity", methods=["GET"])
def activity():
    return jsonify(_activity_data())
