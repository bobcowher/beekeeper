"""Pipeline execution: a state machine persisted in SQLite, advanced by dispatcher.tick().

Talks to the core only through the run queue, run records, process_manager.is_run_live
and process_manager.stop_training, so the same contract can later target a remote host.
"""
import datetime
import logging
import os
import threading
import time

from services import pipeline_tasks, process_manager
from services.config_service import get_config_bool
from services.db_service import get_db
from services.pipeline_schema import PROJECT_NAME_RE, step_dir_var
from services.pipeline_store import load_pipeline, pipeline_run_dir
from services.run_refs import run_ref
from services.run_storage_service import persistent_runs_root

log = logging.getLogger(__name__)

INLINE_TASKS_KEY = "pipelines.inline_tasks.enabled"
LOST_GRACE_S = 60
ACTIVE_STEP_STATUSES = {"pending", "queued", "running"}

_engine_lock = threading.RLock()
_not_live_since: dict[int, float] = {}   # step_pk -> first time its run was seen not live


class PipelineError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def pipeline_ref(name, n) -> str:
    return f"{name}#{n}"


def _now() -> str:
    return datetime.datetime.now().isoformat()


def _tasks_enabled() -> bool:
    return get_config_bool(INLINE_TASKS_KEY, True)


def _check_runnable(projects_dir, steps):
    if any(s["type"] == "task" for s in steps) and not _tasks_enabled():
        raise PipelineError("TASKS_DISABLED", "Inline task steps are disabled by the administrator", 403)
    invalid = sorted({s["project"] for s in steps if s["type"] == "train"
                      and not PROJECT_NAME_RE.fullmatch(s["project"])})
    if invalid:
        raise PipelineError("INVALID_PROJECT_REF",
                            f"Pipeline contains invalid project reference(s): {', '.join(invalid)}")
    missing = sorted({s["project"] for s in steps if s["type"] == "train"
                      and not os.path.isfile(os.path.join(projects_dir, s["project"], "project.json"))})
    if missing:
        raise PipelineError("PROJECT_NOT_FOUND", f"Pipeline references missing project(s): {', '.join(missing)}")


def _get_run(name, run_number) -> dict:
    run = get_db().get_pipeline_run(name, run_number)
    if run is None:
        raise PipelineError("NOT_FOUND", f"Pipeline run {pipeline_ref(name, run_number)} not found", 404)
    return run


# --- Public operations -----------------------------------------------------------

def start_pipeline_run(projects_dir, pipelines_dir, name) -> dict:
    try:
        defn, _ = load_pipeline(pipelines_dir, name)
    except FileNotFoundError:
        raise PipelineError("NOT_FOUND", f"Pipeline '{name}' not found", 404)
    _check_runnable(projects_dir, defn["steps"])
    with _engine_lock:
        created = get_db().create_pipeline_run(name, defn)
    if created is None:
        raise PipelineError("PIPELINE_ACTIVE", f"Pipeline '{name}' already has a running run", 409)
    _, n = created
    os.makedirs(pipeline_run_dir(pipelines_dir, name, n), exist_ok=True)
    log.info("Pipeline %s started", pipeline_ref(name, n))
    return {"run_id": n, "run_ref": pipeline_ref(name, n), "status": "running"}


def cancel_pipeline_run(projects_dir, pipelines_dir, name, run_number) -> dict:
    db = get_db()
    with _engine_lock:
        run = _get_run(name, run_number)
        if run["status"] != "running":
            raise PipelineError("NOT_RUNNING", f"{pipeline_ref(name, run_number)} is {run['status']}", 409)
        sr = db.current_step_run(run["id"], run["current_step"])
        if sr and sr["status"] == "succeeded":
            # Finished but never advanced (crash between writes): settle first, so a
            # finished pipeline is not recorded as cancelled.
            _settle(run, sr)
            run = db.get_pipeline_run_by_pk(run["id"])
            sr = db.current_step_run(run["id"], run["current_step"])
        if run["status"] == "running":
            if sr is None:
                step = run["definition"]["steps"][run["current_step"]]
                db.create_step_run(run["id"], run["current_step"], step["name"], step["type"])
                sr = db.current_step_run(run["id"], run["current_step"])
            if sr["status"] in ACTIVE_STEP_STATUSES:
                _stop_active_step(projects_dir, sr)
                _finish_step(run, sr, "cancelled", error="cancelled by user")
            else:
                _settle(run, sr)   # terminal but unsettled: record its outcome, skip the rest
        final = db.get_pipeline_run_by_pk(run["id"])["status"]
    return {"run_id": run_number, "run_ref": pipeline_ref(name, run_number), "status": final}


def resume_pipeline_run(projects_dir, pipelines_dir, name, run_number) -> dict:
    db = get_db()
    with _engine_lock:
        run = _get_run(name, run_number)
        if run["status"] not in ("failed", "cancelled"):
            raise PipelineError("NOT_RESUMABLE", f"{pipeline_ref(name, run_number)} is {run['status']}", 409)
        latest = db.latest_pipeline_run(name)
        if latest is None or latest["id"] != run["id"]:
            raise PipelineError("NOT_LATEST", "Only the latest run of a pipeline can be resumed", 409)
        idx = run["current_step"]
        steps = run["definition"]["steps"]
        for s in db.list_step_runs(run["id"]):
            if s["step_index"] < idx and s["status"] == "succeeded" and not os.path.isdir(s["run_dir"] or ""):
                raise PipelineError("ARTIFACTS_MISSING",
                                    f"Output of step '{s['step_name']}' no longer exists ({s['run_dir']})", 409)
        _check_runnable(projects_dir, steps[idx:])
        db.delete_skipped_steps(run["id"])
        db.create_step_run(run["id"], idx, steps[idx]["name"], steps[idx]["type"])
        db.update_pipeline_run(run["id"], status="running", error=None, ended_at=None)
    log.info("Pipeline %s resumed at step %s", pipeline_ref(name, run_number), steps[idx]["name"])
    return {"run_id": run_number, "run_ref": pipeline_ref(name, run_number), "status": "running"}


def tick(projects_dir, pipelines_dir) -> None:
    for run in get_db().running_pipeline_runs():
        try:
            with _engine_lock:
                fresh = get_db().get_pipeline_run_by_pk(run["id"])
                if fresh and fresh["status"] == "running":
                    _advance(projects_dir, pipelines_dir, fresh)
        except Exception:
            log.exception("Pipeline %s failed to advance", pipeline_ref(run["pipeline_name"], run["run_number"]))


def handoff_env(pipelines_dir, run, step_runs, step_index) -> dict:
    name, n = run["pipeline_name"], run["run_number"]
    env = {
        "BEEKEEPER_PIPELINE": name,
        "BEEKEEPER_PIPELINE_RUN_ID": str(n),
        "BEEKEEPER_PIPELINE_RUN_DIR": pipeline_run_dir(pipelines_dir, name, n),
    }
    by_index = {s["step_index"]: s for s in step_runs}
    for i in range(step_index):
        s = by_index.get(i)
        if s and s["status"] == "succeeded" and s["run_dir"]:
            env[step_dir_var(s["step_name"])] = s["run_dir"]
    prev = by_index.get(step_index - 1)
    if prev and prev["status"] == "succeeded" and prev["run_dir"]:
        env["BEEKEEPER_PREV_STEP_DIR"] = prev["run_dir"]
    return env


def describe_run(run) -> dict:
    db = get_db()
    by_index = {s["step_index"]: s for s in db.list_step_runs(run["id"])}
    steps = []
    for i, step in enumerate(run["definition"]["steps"]):
        s = by_index.get(i)
        view = {
            "index": i, "name": step["name"], "type": step["type"],
            "status": s["status"] if s else "pending",
            "attempt": s["attempt"] if s else 0,
            "error": s["error"] if s else None,
            "exit_code": s["exit_code"] if s else None,
            "started_at": s["started_at"] if s else None,
            "ended_at": s["ended_at"] if s else None,
            "run_dir": s["run_dir"] if s else None,
        }
        if step["type"] == "train":
            view["project"] = step["project"]
            if s and s["run_pk"]:
                tr = db.get_training_run(s["run_pk"])
                if tr:
                    view["run_id"] = tr["run_number"]
                    view["run_ref"] = run_ref(step["project"], tr["run_number"])
            if s and s["status"] == "queued" and s["queue_id"]:
                entry = db.get_queue_entry(s["queue_id"])
                view["wait_reason"] = entry["wait_reason"] if entry else None
        else:
            view["has_log"] = bool(s and s["log_path"])
        steps.append(view)
    return {
        "run_id": run["run_number"], "run_ref": pipeline_ref(run["pipeline_name"], run["run_number"]),
        "pipeline": run["pipeline_name"], "status": run["status"], "current_step": run["current_step"],
        "error": run["error"], "started_at": run["started_at"], "ended_at": run["ended_at"], "steps": steps,
    }


def running_task_labels() -> list[str]:
    pks = set(pipeline_tasks.running_task_pks())
    if not pks:
        return []
    labels = []
    for run in get_db().running_pipeline_runs():
        sr = get_db().current_step_run(run["id"], run["current_step"])
        if sr and sr["id"] in pks:
            labels.append(f"{pipeline_ref(run['pipeline_name'], run['run_number'])}:{sr['step_name']}")
    return labels


# --- State machine ---------------------------------------------------------------

def _stop_training_run(projects_dir, run_pk):
    tr = get_db().get_training_run(run_pk)
    if tr:
        process_manager.stop_training(projects_dir, tr["project_name"], run_id=tr["run_number"])


def _stop_active_step(projects_dir, sr):
    """Stop whatever an active step has in flight (queue entry, training run or task)."""
    db = get_db()
    if sr["status"] == "queued":
        if not db.cancel_queue_entry(sr["queue_id"], error="pipeline cancelled"):
            entry = db.get_queue_entry(sr["queue_id"])
            if entry and entry["status"] == "started":
                _stop_training_run(projects_dir, entry["run_pk"])
    elif sr["status"] == "running":
        if sr["step_type"] == "train":
            _stop_training_run(projects_dir, sr["run_pk"])
        else:
            pipeline_tasks.kill_task(sr["id"])
    elif sr["step_type"] == "task":
        pipeline_tasks.kill_task(sr["id"])   # pending, but a spawn whose step write failed may exist


def _advance(projects_dir, pipelines_dir, run):
    db = get_db()
    idx = run["current_step"]
    step = run["definition"]["steps"][idx]
    sr = db.current_step_run(run["id"], idx)
    if sr is None:
        db.create_step_run(run["id"], idx, step["name"], step["type"])
        return
    status = sr["status"]
    if status == "pending":
        _launch(projects_dir, pipelines_dir, run, step, sr)
    elif status == "queued":
        _check_queued(projects_dir, run, sr)
    elif status == "running" and step["type"] == "train":
        _check_train(run, sr)
    elif status == "running":
        _check_task(run, step, sr)
    else:
        _settle(run, sr)   # finished but the pipeline row wasn't advanced (crash between writes)


def _launch(projects_dir, pipelines_dir, run, step, sr):
    db = get_db()
    idx = run["current_step"]
    env = handoff_env(pipelines_dir, run, db.list_step_runs(run["id"]), idx)
    name, n = run["pipeline_name"], run["run_number"]
    if step["type"] == "train":
        if not PROJECT_NAME_RE.fullmatch(step["project"]):
            _finish_step(run, sr, "failed", error=f"invalid project reference: {step['project']!r}")
            return
        extra = dict(step.get("env_vars") or {})
        extra.update(env)
        # One transaction: a failed step write must not leave an orphan queue entry behind.
        db.enqueue_step_run(sr["id"], step["project"], step.get("branch"), extra,
                            f"pipeline:{pipeline_ref(name, n)}")
        return
    run_dir = env["BEEKEEPER_PIPELINE_RUN_DIR"]
    suffix = f"_attempt{sr['attempt']}" if sr["attempt"] > 1 else ""
    log_rel = os.path.join(name, "runs", f"run_{n}", f"step_{idx}_{step['name']}{suffix}.log")
    if sr["id"] in pipeline_tasks.running_task_pks():
        # Spawned on an earlier tick whose step write failed: record it, never spawn twice.
        db.update_step_run(sr["id"], status="running", log_path=log_rel,
                           run_dir=run_dir, started_at=_now())
        return
    if not _tasks_enabled():
        _finish_step(run, sr, "failed", error="Inline task steps are disabled by the administrator")
        return
    proc_env = os.environ.copy()
    proc_env.update(env)
    try:
        pid = pipeline_tasks.spawn_task(sr["id"], step["command"], run_dir, proc_env,
                                        os.path.join(pipelines_dir, log_rel), step["timeout_s"])
    except OSError as e:
        _finish_step(run, sr, "failed", error=f"could not start task: {e}")
        return
    db.update_step_run(sr["id"], status="running", pid=pid, log_path=log_rel,
                       run_dir=run_dir, started_at=_now())


def _check_queued(projects_dir, run, sr):
    db = get_db()
    entry = db.get_queue_entry(sr["queue_id"])
    if entry is None or entry["status"] == "cancelled":
        _finish_step(run, sr, "failed", error="queued run was cancelled")
    elif entry["status"] == "failed":
        _finish_step(run, sr, "failed", error=entry["error"])
    elif entry["status"] == "started":
        tr = db.get_training_run(entry["run_pk"])
        if tr is None:
            _finish_step(run, sr, "failed", error="training run record was deleted")
            return
        run_dir = os.path.join(persistent_runs_root(projects_dir, entry["project_name"]),
                               f"run_{tr['run_number']}")
        db.update_step_run(sr["id"], status="running", run_pk=entry["run_pk"],
                           run_dir=run_dir, started_at=_now())


def _check_train(run, sr):
    tr = get_db().get_training_run(sr["run_pk"])
    if tr is None:
        _finish_step(run, sr, "failed", error="training run record was deleted")
        return
    if tr["status"] == "completed":
        _not_live_since.pop(sr["id"], None)
        _finish_step(run, sr, "succeeded", exit_code=tr.get("exit_code"))
    elif tr["status"] in ("crashed", "canceled"):
        _not_live_since.pop(sr["id"], None)
        _finish_step(run, sr, "failed", exit_code=tr.get("exit_code"), error=f"training run {tr['status']}")
    elif process_manager.is_run_live(sr["run_pk"]):
        _not_live_since.pop(sr["id"], None)
    else:
        first = _not_live_since.setdefault(sr["id"], time.time())
        if time.time() - first >= LOST_GRACE_S:
            _not_live_since.pop(sr["id"], None)
            _finish_step(run, sr, "failed", error="run lost (Beekeeper restarted while it was running)")


def _check_task(run, step, sr):
    outcome, code = pipeline_tasks.poll_task(sr["id"])
    if outcome == "running":
        return
    if outcome == "missing":
        _finish_step(run, sr, "failed", error="interrupted by restart")
    elif outcome == "timed_out":
        _finish_step(run, sr, "failed", exit_code=code, error=f"timed out after {step['timeout_s']}s")
    elif code == 0:
        _finish_step(run, sr, "succeeded", exit_code=0)
    else:
        _finish_step(run, sr, "failed", exit_code=code, error=f"exit code {code}")


def _finish_step(run, sr, status, exit_code=None, error=None):
    get_db().update_step_run(sr["id"], status=status, exit_code=exit_code, error=error, ended_at=_now())
    _settle(run, dict(sr, status=status, error=error))


def _settle(run, sr):
    """Move the pipeline on from a finished step. Idempotent."""
    db = get_db()
    idx = run["current_step"]
    steps = run["definition"]["steps"]
    if sr["status"] == "succeeded":
        if idx + 1 < len(steps):
            nxt = steps[idx + 1]
            if db.current_step_run(run["id"], idx + 1) is None:
                db.create_step_run(run["id"], idx + 1, nxt["name"], nxt["type"])
            db.update_pipeline_run(run["id"], current_step=idx + 1)
        else:
            db.update_pipeline_run(run["id"], status="succeeded", ended_at=_now())
            log.info("Pipeline %s succeeded", pipeline_ref(run["pipeline_name"], run["run_number"]))
        return
    for j in range(idx + 1, len(steps)):
        if db.current_step_run(run["id"], j) is None:
            db.create_step_run(run["id"], j, steps[j]["name"], steps[j]["type"], status="skipped")
    final = "cancelled" if sr["status"] == "cancelled" else "failed"
    db.update_pipeline_run(run["id"], status=final, ended_at=_now(),
                           error=f"step '{steps[idx]['name']}': {sr.get('error') or sr['status']}")
    log.info("Pipeline %s %s at step %s", pipeline_ref(run["pipeline_name"], run["run_number"]),
             final, steps[idx]["name"])
