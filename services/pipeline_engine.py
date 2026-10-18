"""Pipeline execution: a state machine persisted in SQLite, advanced by dispatcher.tick().

Talks to the core only through the run queue, run records, process_manager.is_run_live
and process_manager.stop_training, so the same contract can later target a remote host.
"""
import copy
import datetime
import logging
import os
import shutil
import threading
import time

from services import inherit as inherit_service
from services import pipeline_checks, pipeline_tasks, process_manager
from services.config_service import get_config_bool
from services.db_service import get_db
from services.pipeline_schema import PROJECT_NAME_RE, step_dir_var, valid_branch_name
from services.pipeline_store import delete_pipeline_files, load_pipeline, pipeline_exists, pipeline_run_dir
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


def _project_step(step) -> bool:
    """A train step or a project task: it runs as a run of its project, through the project queue."""
    return step["type"] == "train" or bool(step.get("project"))


def _check_errors_message(errors) -> str:
    return "; ".join(f"{e['path']}: {e['message']}" for e in errors)


def _check_runnable(projects_dir, steps, start=0):
    """Refuse a start (or resume at steps[start]) that can't run. steps is the whole definition."""
    todo = steps[start:]
    if any(s["type"] == "task" for s in todo) and not _tasks_enabled():
        raise PipelineError("TASKS_DISABLED", "Inline task steps are disabled by the administrator", 403)
    invalid = sorted({s["project"] for s in todo if _project_step(s)
                      and not PROJECT_NAME_RE.fullmatch(s["project"])})
    if invalid:
        raise PipelineError("INVALID_PROJECT_REF",
                            f"Pipeline contains invalid project reference(s): {', '.join(invalid)}")
    missing = sorted({s["project"] for s in todo if _project_step(s)
                      and not os.path.isfile(os.path.join(projects_dir, s["project"], "project.json"))})
    if missing:
        raise PipelineError("PROJECT_NOT_FOUND", f"Pipeline references missing project(s): {', '.join(missing)}")
    # Project settings may have changed since the pipeline was saved; fail now, not hours in.
    errors = pipeline_checks.check_steps(projects_dir, steps, indices=range(start, len(steps)))
    if errors:
        code = next((e["error_code"] for e in errors if e.get("error_code")), "INVALID_PIPELINE")
        raise PipelineError(code, _check_errors_message(errors))


def _wake_dispatcher():
    from services import dispatcher  # lazy: dispatcher imports this module
    dispatcher.wake()


def _get_run(name, run_number) -> dict:
    run = get_db().get_pipeline_run(name, run_number)
    if run is None:
        raise PipelineError("NOT_FOUND", f"Pipeline run {pipeline_ref(name, run_number)} not found", 404)
    return run


# --- Public operations -----------------------------------------------------------

def _apply_branch_overrides(defn, branches):
    if branches is None:
        branches = {}
    if not isinstance(branches, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in branches.items()):
        raise PipelineError("INVALID_OVERRIDE", "branches must map step names to branch names")
    steps = {step["name"]: step for step in defn["steps"]}
    overrides = {}
    for step_name, branch in branches.items():
        step = steps.get(step_name)
        if step is None or step["type"] != "train":
            raise PipelineError("INVALID_OVERRIDE",
                                f"'{step_name}' is not a train step in this pipeline")
        if not branch:
            continue
        if not valid_branch_name(branch):
            raise PipelineError("INVALID_BRANCH", f"'{branch}' is not a valid branch name")
        step["branch"] = branch
        overrides[step_name] = branch
    return overrides


def start_pipeline_run(projects_dir, pipelines_dir, name, branches=None) -> dict:
    with _engine_lock:
        try:
            defn, _ = load_pipeline(pipelines_dir, name)
        except FileNotFoundError:
            raise PipelineError("NOT_FOUND", f"Pipeline '{name}' not found", 404)
        defn = copy.deepcopy(defn)
        overrides = _apply_branch_overrides(defn, branches)
        _check_runnable(projects_dir, defn["steps"])
        pk, n, status = get_db().create_pipeline_run(name, defn, overrides)
        os.makedirs(pipeline_run_dir(pipelines_dir, name, n), exist_ok=True)
        result = {"run_id": n, "run_ref": pipeline_ref(name, n), "status": status}
        if status == "queued":
            result["position"] = next(
                i for i, run in enumerate(get_db().queued_pipeline_runs(name), 1) if run["id"] == pk)
    log.info("Pipeline %s %s", pipeline_ref(name, n), status)
    _wake_dispatcher()
    return result


def delete_pipeline(pipelines_dir, name) -> None:
    """Delete a stopped pipeline without racing a simultaneous run start."""
    with _engine_lock:
        if not pipeline_exists(pipelines_dir, name):
            raise PipelineError("NOT_FOUND", f"Pipeline '{name}' not found", 404)
        if get_db().has_active_pipeline_run(name):
            raise PipelineError("PIPELINE_ACTIVE", f"Cancel active runs of pipeline '{name}' first", 409)
        delete_pipeline_files(pipelines_dir, name)
        get_db().delete_pipeline_records(name)


def cancel_pipeline_run(projects_dir, pipelines_dir, name, run_number) -> dict:  # NOSONAR - shared handler signature
    db = get_db()
    with _engine_lock:
        run = _get_run(name, run_number)
        if run["status"] == "queued":
            db.update_pipeline_run(run["id"], status="cancelled", error="cancelled by user",
                                   ended_at=_now())
            final = "cancelled"
            _wake_dispatcher()
            return {"run_id": run_number, "run_ref": pipeline_ref(name, run_number),
                    "status": final}
        if run["status"] != "running":
            raise PipelineError("NOT_RUNNING", f"{pipeline_ref(name, run_number)} is {run['status']}", 409)
        sr = _live_step_run(db, run["id"], run["current_step"])
        if sr and sr["status"] == "succeeded":
            # Finished but never advanced (crash between writes): settle first, so a
            # finished pipeline is not recorded as cancelled.
            _settle(run, sr)
            run = db.get_pipeline_run_by_pk(run["id"])
            sr = _live_step_run(db, run["id"], run["current_step"])
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


def _check_resumable(db, run, from_step):
    name, run_number = run["pipeline_name"], run["run_number"]
    if from_step is not None and not isinstance(from_step, str):
        raise PipelineError("INVALID_STEP", "from_step must be a step name", 400)
    resumable_statuses = ("failed", "cancelled", "succeeded") if from_step is not None else ("failed", "cancelled")
    if run["status"] not in resumable_statuses:
        raise PipelineError("NOT_RESUMABLE", f"{pipeline_ref(name, run_number)} is {run['status']}", 409)
    if any(r["pipeline_name"] == name for r in db.running_pipeline_runs()):
        raise PipelineError("PIPELINE_ACTIVE", f"Pipeline '{name}' already has a running run", 409)
    latest = db.latest_pipeline_run(name)
    if latest is None or latest["id"] != run["id"]:
        raise PipelineError("NOT_LATEST", "Only the latest run of a pipeline can be resumed", 409)


def _resume_index(run, from_step):
    """The step a resume restarts at: where the run stopped, or the named step that already ran."""
    if from_step is None:
        return run["current_step"]
    steps = run["definition"]["steps"]
    idx = next((i for i, step in enumerate(steps) if step["name"] == from_step), None)
    if idx is None:
        raise PipelineError("INVALID_STEP", f"Unknown step '{from_step}'", 400)
    if idx > run["current_step"]:
        raise PipelineError("NOT_RESUMABLE", f"step '{from_step}' has not run yet", 409)
    return idx


def _check_artifacts(db, run, idx):
    for s in db.list_step_runs(run["id"]):
        if s["step_index"] < idx and s["status"] == "succeeded" and not os.path.isdir(s["run_dir"] or ""):
            raise PipelineError("ARTIFACTS_MISSING",
                                f"Output of step '{s['step_name']}' no longer exists ({s['run_dir']})", 409)


def _supersede_after(db, run, idx):
    """Mark later steps' current attempts so a re-run runs them again instead of reusing them."""
    for j in range(idx + 1, len(run["definition"]["steps"])):
        later = db.current_step_run(run["id"], j)
        if later is not None and later["status"] != "superseded":
            db.update_step_run(later["id"], status="superseded")


def resume_pipeline_run(projects_dir, pipelines_dir, name, run_number,  # NOSONAR - shared handler signature
                        from_step=None) -> dict:
    db = get_db()
    with _engine_lock:
        run = _get_run(name, run_number)
        _check_resumable(db, run, from_step)
        idx = _resume_index(run, from_step)
        steps = run["definition"]["steps"]
        _check_artifacts(db, run, idx)
        _check_runnable(projects_dir, steps, start=idx)
        db.delete_skipped_steps(run["id"])
        _supersede_after(db, run, idx)
        db.create_step_run(run["id"], idx, steps[idx]["name"], steps[idx]["type"])
        db.update_pipeline_run(run["id"], status="running", current_step=idx, error=None,
                               ended_at=None, dismissed=0)
    log.info("Pipeline %s resumed at step %s", pipeline_ref(name, run_number), steps[idx]["name"])
    _wake_dispatcher()
    return {"run_id": run_number, "run_ref": pipeline_ref(name, run_number), "status": "running"}


def dismiss_pipeline_run(projects_dir, pipelines_dir, name, run_number) -> dict:  # NOSONAR - shared handler signature
    """Leave a failed run as it is and let the queued runs behind it start."""
    db = get_db()
    with _engine_lock:
        run = _get_run(name, run_number)
        if run["status"] != "failed":
            raise PipelineError("NOT_DISMISSABLE", f"{pipeline_ref(name, run_number)} is {run['status']}", 409)
        latest = db.latest_pipeline_run(name)
        if latest is None or latest["id"] != run["id"]:
            raise PipelineError("NOT_LATEST", "Only the latest run of a pipeline holds its queue", 409)
        db.update_pipeline_run(run["id"], dismissed=1)
    log.info("Pipeline %s dismissed; queue continues", pipeline_ref(name, run_number))
    _wake_dispatcher()
    return {"run_id": run_number, "run_ref": pipeline_ref(name, run_number), "status": run["status"]}


def keep_pipeline_run(projects_dir, pipelines_dir, name, run_number, keep=True) -> dict:  # NOSONAR - shared handler signature
    """Protect a run's step runs from history pruning, or hand them back to it."""
    db = get_db()
    with _engine_lock:
        run = _get_run(name, run_number)
        db.update_pipeline_run(run["id"], kept=1 if keep else 0)
    log.info("Pipeline %s %s", pipeline_ref(name, run_number), "kept" if keep else "no longer kept")
    return {"run_id": run_number, "run_ref": pipeline_ref(name, run_number), "kept": bool(keep)}


MAX_ADVANCES_PER_TICK = 50   # bounds the loop below if a step never stops changing


def _progress_key(run):
    sr = get_db().current_step_run(run["id"], run["current_step"])
    return run["status"], run["current_step"], sr and (sr["id"], sr["status"])


def _promote_queued_pipeline(projects_dir, name):
    db = get_db()
    with _engine_lock:
        promoted = db.promote_pipeline_run(name)
        if not promoted:
            return
        try:
            _check_runnable(projects_dir, promoted["definition"]["steps"])
        except PipelineError as e:
            sr = db.current_step_run(promoted["id"], 0)
            _finish_step(promoted, sr, "failed", error=e.message)


def _advance_running_pipeline(projects_dir, pipelines_dir, run):
    with _engine_lock:
        # Keep advancing while state changes, so finishing one step launches
        # the next on this tick rather than DISPATCH_INTERVAL later.
        for _ in range(MAX_ADVANCES_PER_TICK):
            fresh = get_db().get_pipeline_run_by_pk(run["id"])
            if not fresh or fresh["status"] != "running":
                break
            before = _progress_key(fresh)
            _advance(projects_dir, pipelines_dir, fresh)
            after = get_db().get_pipeline_run_by_pk(run["id"])
            if not after or _progress_key(after) == before:
                break


def tick(projects_dir, pipelines_dir) -> None:
    db = get_db()
    for name in db.pipelines_with_queued_runs():
        try:
            _promote_queued_pipeline(projects_dir, name)
        except Exception:
            log.exception("Queued pipeline %s failed to promote", name)
    for run in db.running_pipeline_runs():
        try:
            _advance_running_pipeline(projects_dir, pipelines_dir, run)
        except Exception:
            log.exception("Pipeline %s failed to advance", pipeline_ref(run["pipeline_name"], run["run_number"]))


def handoff_env(pipelines_dir, run, step_runs, step_index, inherits=False) -> dict:
    """The BEEKEEPER_PIPELINE_* variables, plus the step-folder variables unless the step inherits (R5)."""
    name, n = run["pipeline_name"], run["run_number"]
    env = {
        "BEEKEEPER_PIPELINE": name,
        "BEEKEEPER_PIPELINE_RUN_ID": str(n),
        "BEEKEEPER_PIPELINE_RUN_DIR": pipeline_run_dir(pipelines_dir, name, n),
    }
    if inherits:
        # Its inputs are in its own run folder. Pointing at the others would also make a
        # Runpod step upload every earlier step's folder for variables it doesn't use.
        return env
    by_index = {s["step_index"]: s for s in step_runs}
    for i in range(step_index):
        s = by_index.get(i)
        if s and s["status"] == "succeeded" and s["run_dir"]:
            env[step_dir_var(s["step_name"])] = s["run_dir"]
    prev = by_index.get(step_index - 1)
    if prev and prev["status"] == "succeeded" and prev["run_dir"]:
        env["BEEKEEPER_PREV_STEP_DIR"] = prev["run_dir"]
    return env


def _describe_train_step(db, step, step_run, view):
    view["project"] = step["project"]
    view["branch"] = step.get("branch")
    if step.get("script"):
        view["script"] = step["script"]
    if step_run and step_run["run_pk"]:
        training_run = db.get_training_run(step_run["run_pk"])
        if training_run:
            view["run_id"] = training_run["run_number"]
            view["run_ref"] = run_ref(step["project"], training_run["run_number"])
    if step_run and step_run["status"] == "queued" and step_run["queue_id"]:
        entry = db.get_queue_entry(step_run["queue_id"])
        view["wait_reason"] = entry["wait_reason"] if entry else None
        view["queue_position"] = db.queue_position(step_run["queue_id"])


def _shown_step_status(step_run):
    """A step a re-run will run again (superseded) shows as pending, like one not reached yet."""
    if step_run is None or step_run["status"] == "superseded":
        return "pending"
    return step_run["status"]


def _describe_step(db, index, step, step_run):
    view = {
        "index": index, "name": step["name"], "type": step["type"],
        "status": _shown_step_status(step_run),
        "attempt": step_run["attempt"] if step_run else 0,
        "error": step_run["error"] if step_run else None,
        "exit_code": step_run["exit_code"] if step_run else None,
        "started_at": step_run["started_at"] if step_run else None,
        "ended_at": step_run["ended_at"] if step_run else None,
        "run_dir": step_run["run_dir"] if step_run else None,
    }
    if step.get("inherit"):
        view["inherit"] = step["inherit"]
        view["inherited"] = inherit_service.read_manifest(step_run["run_dir"]) \
            if step_run and step_run["run_dir"] and step_run["status"] != "superseded" else []
    if _project_step(step):
        _describe_train_step(db, step, step_run, view)
    else:
        view["has_log"] = bool(step_run and step_run["log_path"])
    return view


def describe_run(run) -> dict:
    db = get_db()
    by_index = {s["step_index"]: s for s in db.list_step_runs(run["id"])}
    steps = [
        _describe_step(db, index, step, by_index.get(index))
        for index, step in enumerate(run["definition"]["steps"])
    ]
    return {
        "run_id": run["run_number"], "run_ref": pipeline_ref(run["pipeline_name"], run["run_number"]),
        "pipeline": run["pipeline_name"], "status": run["status"], "current_step": run["current_step"],
        "error": run["error"], "overrides": run["overrides"], "dismissed": run["dismissed"],
        "kept": run["kept"],
        "started": bool(by_index),   # false for queued runs and runs removed from the queue
        # Running, but only waiting in a project's queue: nothing is executing yet.
        "waiting": run["status"] == "running" and steps[run["current_step"]]["status"] == "queued",
        "started_at": run["started_at"], "ended_at": run["ended_at"], "steps": steps,
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

def _live_step_run(db, pipeline_run_pk, step_index):
    """Return the current attempt unless it was invalidated by a re-run."""
    step_run = db.current_step_run(pipeline_run_pk, step_index)
    return None if step_run and step_run["status"] == "superseded" else step_run


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
        if sr["run_pk"]:   # a train step or a project task: a run of its project
            _stop_training_run(projects_dir, sr["run_pk"])
        else:
            pipeline_tasks.kill_task(sr["id"])
    elif sr["step_type"] == "task":
        pipeline_tasks.kill_task(sr["id"])   # pending, but a spawn whose step write failed may exist


def _advance(projects_dir, pipelines_dir, run):
    db = get_db()
    idx = run["current_step"]
    step = run["definition"]["steps"][idx]
    sr = _live_step_run(db, run["id"], idx)
    if sr is None:
        db.create_step_run(run["id"], idx, step["name"], step["type"])
        return
    status = sr["status"]
    if status == "pending":
        _launch(projects_dir, pipelines_dir, run, step, sr)
    elif status == "queued":
        _check_queued(projects_dir, run, sr)
    elif status == "running" and _project_step(step):
        _check_train(run, step, sr)
    elif status == "running":
        _check_task(run, step, sr)
    else:
        _settle(run, sr)   # finished but the pipeline row wasn't advanced (crash between writes)


def _inherit_source(db, run, idx) -> tuple[dict | None, str | None]:
    """({source_run_dir, source_step, source_run_ref}, error) for what step idx inherits.

    The source is the live (non-superseded) attempt of the step above, so a resume or a
    re-run from an earlier step inherits from whatever that step produced last.
    """
    prev = _live_step_run(db, run["id"], idx - 1)
    if prev is None or prev["status"] != "succeeded" or not prev["run_dir"]:
        return None, "inherit: the previous step has no finished run folder"
    ref = f"{pipeline_ref(run['pipeline_name'], run['run_number'])}:{prev['step_name']}"
    if prev["run_pk"]:
        tr = db.get_training_run(prev["run_pk"])
        if tr:
            ref = run_ref(tr["project_name"], tr["run_number"])
    return {"source_run_dir": prev["run_dir"], "source_step": prev["step_name"], "source_run_ref": ref}, None


def _launch_errors(projects_dir, run, idx, step) -> tuple[dict | None, str | None]:
    """Repeat the save-time checks (settings may have changed) and find the inherit source.

    Returns (source or None, error). Runs before anything is queued or any pod is rented.
    """
    errors = pipeline_checks.check_steps(projects_dir, run["definition"]["steps"], indices=[idx])
    if errors:
        return None, _check_errors_message(errors)
    if not step.get("inherit"):
        return None, None
    source, error = _inherit_source(get_db(), run, idx)
    if error:
        return None, error
    missing = inherit_service.missing_paths(source["source_run_dir"], step["inherit"])
    if missing:
        return None, f"inherit: {missing[0]} not found in {source['source_run_ref']}'s run folder"
    return source, None


def _launch(projects_dir, pipelines_dir, run, step, sr):
    db = get_db()
    idx = run["current_step"]
    env = handoff_env(pipelines_dir, run, db.list_step_runs(run["id"]), idx, inherits=bool(step.get("inherit")))
    if _project_step(step):
        _launch_project_step(projects_dir, run, step, sr, env)
    else:
        _launch_task(projects_dir, pipelines_dir, run, step, sr, env)


def _launch_project_step(projects_dir, run, step, sr, env):
    """A train step or a project task: queue a run of the project (R1)."""
    db = get_db()
    name, n = run["pipeline_name"], run["run_number"]
    if not PROJECT_NAME_RE.fullmatch(step["project"]):
        _finish_step(run, sr, "failed", error=f"invalid project reference: {step['project']!r}")
        return
    if step["type"] == "task" and not _tasks_enabled():
        _finish_step(run, sr, "failed", error="Inline task steps are disabled by the administrator")
        return
    source, error = _launch_errors(projects_dir, run, run["current_step"], step)
    if error:
        _finish_step(run, sr, "failed", error=error)
        return
    extra = dict(step.get("env_vars") or {})
    extra.update(env)
    launch = {"kind": step["type"]}
    if step["type"] == "train":
        launch["script"] = step.get("script")
    else:
        launch.update(command=step["command"], timeout_s=step["timeout_s"])
    if source:
        launch.update(source, inherit=step["inherit"])
    # One transaction: a failed step write must not leave an orphan queue entry behind.
    db.enqueue_step_run(sr["id"], step["project"], step.get("branch"), extra,
                        f"pipeline:{pipeline_ref(name, n)}", launch=launch)
    _wake_dispatcher()   # the queue drain ran earlier in this tick; take the entry on the next one now


def _seed_step_folder(run_dir, step, source) -> list[str]:
    """A plain task with inherit gets a fresh folder holding just the inherited paths (R4)."""
    if os.path.isdir(run_dir):
        shutil.rmtree(run_dir)   # an earlier attempt's folder; nothing else points into it
    os.makedirs(run_dir)
    entries = inherit_service.seed_inherited(source["source_run_dir"], run_dir, step["inherit"],
                                             source["source_step"], source["source_run_ref"])
    return inherit_service.log_lines(entries)


def _launch_task(projects_dir, pipelines_dir, run, step, sr, env):
    db = get_db()
    idx = run["current_step"]
    name, n = run["pipeline_name"], run["run_number"]
    run_dir = env["BEEKEEPER_PIPELINE_RUN_DIR"]
    if step.get("inherit"):
        run_dir = os.path.join(run_dir, "steps", step["name"])
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
    source, error = _launch_errors(projects_dir, run, idx, step)
    if error:
        _finish_step(run, sr, "failed", error=error)
        return
    preamble = []
    if source:
        try:
            preamble = _seed_step_folder(run_dir, step, source)
        except (inherit_service.InheritError, OSError) as e:
            _finish_step(run, sr, "failed", error=str(e))
            return
    proc_env = os.environ.copy()
    proc_env.update(env)
    try:
        pid = pipeline_tasks.spawn_task(sr["id"], step["command"], run_dir, proc_env,
                                        os.path.join(pipelines_dir, log_rel), step["timeout_s"],
                                        preamble=preamble)
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


def _run_failure(step, tr) -> str:
    if step["type"] == "task":
        if tr.get("exit_code") == process_manager.TASK_TIMEOUT_EXIT:
            return f"timed out after {step['timeout_s']}s"
        return f"task run {tr['status']}"
    return f"training run {tr['status']}"


def _check_train(run, step, sr):
    """A running train step or project task: follow its project run."""
    tr = get_db().get_training_run(sr["run_pk"])
    if tr is None:
        _finish_step(run, sr, "failed", error="training run record was deleted")
        return
    if tr["status"] == "completed":
        _not_live_since.pop(sr["id"], None)
        _finish_step(run, sr, "succeeded", exit_code=tr.get("exit_code"))
    elif tr["status"] in ("crashed", "canceled"):
        _not_live_since.pop(sr["id"], None)
        _finish_step(run, sr, "failed", exit_code=tr.get("exit_code"), error=_run_failure(step, tr))
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
            if _live_step_run(db, run["id"], idx + 1) is None:
                db.create_step_run(run["id"], idx + 1, nxt["name"], nxt["type"])
            db.update_pipeline_run(run["id"], current_step=idx + 1)
        else:
            keep = run["definition"].get("keep") == "on_success"
            db.update_pipeline_run(run["id"], status="succeeded", ended_at=_now(),
                                   **({"kept": 1} if keep else {}))
            log.info("Pipeline %s succeeded%s", pipeline_ref(run["pipeline_name"], run["run_number"]),
                     "; its runs are kept" if keep else "")
        return
    for j in range(idx + 1, len(steps)):
        if _live_step_run(db, run["id"], j) is None:
            db.create_step_run(run["id"], j, steps[j]["name"], steps[j]["type"], status="skipped")
    final = "cancelled" if sr["status"] == "cancelled" else "failed"
    db.update_pipeline_run(run["id"], status=final, ended_at=_now(),
                           error=f"step '{steps[idx]['name']}': {sr.get('error') or sr['status']}")
    log.info("Pipeline %s %s at step %s", pipeline_ref(run["pipeline_name"], run["run_number"]),
             final, steps[idx]["name"])
