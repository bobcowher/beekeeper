"""Rows for the dashboard's Now card and the History page: one per training run or pipeline run."""
import datetime
import json
import re

from services import pipeline_engine, remote_store

_GPU_NOISE = re.compile(r"\s*\(.*\)$|\bNVIDIA\s+|\bGeForce\s+|\s+SFF\b|\s+Generation\b")


def seconds_between(start, end=None):
    if not start:
        return None
    started = datetime.datetime.fromisoformat(start)
    ended = datetime.datetime.fromisoformat(end) if end else datetime.datetime.now()
    return max(0, int((ended - started).total_seconds()))


def where(run: dict | None) -> dict | None:
    """Short label for the GPU(s) a run used, e.g. "RTX 5090" or "Runpod RTX A4000"."""
    if not run or not run.get("gpu_info"):
        return None
    try:
        names = json.loads(run["gpu_info"])
    except (TypeError, ValueError):
        return None
    if not names:
        return None
    label = " + ".join(_GPU_NOISE.sub("", str(n)).strip() for n in names)
    remote = run.get("hostname") == "runpod"
    return {"label": f"Runpod {label}" if remote else label, "remote": remote}


def cost(run: dict | None) -> float | None:
    if not run or run.get("hostname") != "runpod":
        return None
    return round(remote_store.spent_for_run(run["id"]), 4)


def pipeline_steps(db, row, finished: bool) -> tuple[list[dict], str | None, float | None]:
    """The run's steps as a chain, the first failure as "step: error", and its Runpod cost."""
    run_pks = {s["step_index"]: s["run_pk"] for s in db.list_step_runs(row["id"])}
    steps, error, costs = [], None, []
    for view in pipeline_engine.describe_run(row)["steps"]:
        status = view["status"]
        if finished and status == "pending":
            status = "skipped"   # never reached: the pipeline stopped before it
        run = db.get_training_run(run_pks[view["index"]]) if run_pks.get(view["index"]) else None
        run_cost = cost(run)
        if run_cost is not None:
            costs.append(run_cost)
        steps.append({"name": view["name"], "type": view["type"], "status": status, "where": where(run)})
        if error is None and status == "failed":
            error = f"{view['name']}: {view.get('error') or 'failed'}"
    return steps, error, round(sum(costs), 4) if costs else None


def finished_training_item(run: dict) -> dict:
    return {
        "kind": "training", "name": run["project_name"], "run_id": run["run_number"],
        "run_ref": f"{run['project_name']}-{run['run_number']}", "status": run["status"],
        "branch": run.get("branch") or "", "where": where(run), "cost": cost(run),
        "duration": run.get("duration_seconds"),
        "timestamp": run.get("ended_at") or run["started_at"],
        "url": f"/projects/{run['project_name']}/history/{run['run_number']}/log",
    }


def finished_pipeline_item(db, row: dict) -> dict:
    steps, error, run_cost = pipeline_steps(db, row, finished=True)
    return {
        "kind": "pipeline", "name": row["pipeline_name"], "run_id": row["run_number"],
        "run_ref": f"{row['pipeline_name']}#{row['run_number']}",
        "status": "completed" if row["status"] == "succeeded" else row["status"],
        "steps": steps, "error": error, "cost": run_cost,
        "duration": seconds_between(row["started_at"], row.get("ended_at")),
        "timestamp": row.get("ended_at") or row["started_at"],
        "url": f"/pipelines/{row['pipeline_name']}",
    }
