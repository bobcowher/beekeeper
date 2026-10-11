import datetime
import os
import time

import pytest

from services import pipeline_engine as eng
from services import pipeline_store as store
from services.db_service import get_db
from tests.conftest import make_project_dir

PIPE = {"name": "fac", "steps": [
    {"name": "pretrain", "type": "train", "project": "pre"},
    {"name": "fine-tune", "type": "train", "project": "ft", "env_vars": {"LR": "1"}},
    {"name": "deploy", "type": "task", "command": 'test -n "$BEEKEEPER_STEP_FINE_TUNE_DIR"'},
]}


@pytest.fixture
def env(app, monkeypatch):
    make_project_dir(app, "pre")
    make_project_dir(app, "ft")
    store.save_pipeline(app.config["PIPELINES_DIR"], PIPE)
    live = set()
    monkeypatch.setattr(eng.process_manager, "is_run_live", lambda pk: pk in live)
    stops = []
    monkeypatch.setattr(eng.process_manager, "stop_training",
                        lambda d, name, run_id=None: stops.append((name, run_id)) or {"status": "stopped"})
    return {"pd": app.config["PROJECTS_DIR"], "pl": app.config["PIPELINES_DIR"], "live": live, "stops": stops}


def tick(env):
    eng.tick(env["pd"], env["pl"])


def start_queued_run(project):
    """Simulate the dispatcher starting the head queue entry for project."""
    db = get_db()
    entry = db.list_queue(project)[0]
    pk, n = db.create_training_run(project, {"started_at": datetime.datetime.now(), "status": "running"})
    db.mark_queue_started(entry["id"], pk)
    return entry, pk, n


def finish(pk, status="completed", code=0):
    get_db().update_training_run(pk, status=status, exit_code=code)


def step(n_run, idx):
    run = get_db().get_pipeline_run("fac", n_run)
    return get_db().current_step_run(run["id"], idx)


def test_happy_path_hands_off_dirs(env):
    r = eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    assert r == {"run_id": 1, "run_ref": "fac#1", "status": "running"}
    tick(env)                                         # pretrain pending -> queued
    entry, pk, n = start_queued_run("pre")
    assert entry["source"] == "pipeline:fac#1"
    assert entry["env_vars"]["BEEKEEPER_PIPELINE_RUN_ID"] == "1"
    env["live"].add(pk)
    tick(env)                                         # queued -> running
    assert step(1, 0)["status"] == "running"
    pre_dir = step(1, 0)["run_dir"]
    assert pre_dir.endswith(os.path.join("pre", "persistent", "runs", f"run_{n}"))
    finish(pk); env["live"].discard(pk)
    tick(env)                                         # succeeded -> step 1 pending
    tick(env)                                         # step 1 -> queued
    entry, pk2, n2 = start_queued_run("ft")
    assert entry["env_vars"]["LR"] == "1"
    assert entry["env_vars"]["BEEKEEPER_STEP_PRETRAIN_DIR"] == pre_dir
    assert entry["env_vars"]["BEEKEEPER_PREV_STEP_DIR"] == pre_dir
    tick(env); finish(pk2); tick(env)                 # step 1 done -> step 2 pending
    tick(env)                                         # task spawned
    deadline = time.time() + 10
    while get_db().get_pipeline_run("fac", 1)["status"] == "running" and time.time() < deadline:
        tick(env); time.sleep(0.05)
    assert get_db().get_pipeline_run("fac", 1)["status"] == "succeeded"


def test_second_start_refused_while_running(env):
    eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    with pytest.raises(eng.PipelineError) as e:
        eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    assert e.value.code == "PIPELINE_ACTIVE"


def test_missing_project_refused(env, app):
    import shutil
    shutil.rmtree(os.path.join(env["pd"], "ft"))
    with pytest.raises(eng.PipelineError) as e:
        eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    assert e.value.code == "PROJECT_NOT_FOUND"


def test_dispatcher_rejects_unsafe_project_in_persisted_snapshot(env):
    """A pre-upgrade snapshot must not turn a project reference into a path traversal."""
    definition = {"name": "legacy", "steps": [
        {"name": "train", "type": "train", "project": "../outside", "env_vars": {}},
    ]}
    pk, _ = get_db().create_pipeline_run("legacy", definition)

    tick(env)

    run = get_db().get_pipeline_run_by_pk(pk)
    assert run["status"] == "failed"
    assert "invalid project reference" in run["error"]
    assert get_db().projects_with_queued_runs() == []


def test_tasks_disabled_refused(env, monkeypatch):
    monkeypatch.setattr(eng, "get_config_bool", lambda key, default=False: False)
    with pytest.raises(eng.PipelineError) as e:
        eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    assert e.value.code == "TASKS_DISABLED"


def test_crash_fails_pipeline_and_skips_rest(env):
    eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    tick(env)
    _, pk, _ = start_queued_run("pre")
    env["live"].add(pk)
    tick(env)
    finish(pk, "crashed", 1)
    tick(env)
    run = get_db().get_pipeline_run("fac", 1)
    assert run["status"] == "failed" and "pretrain" in run["error"]
    assert [s["status"] for s in get_db().list_step_runs(run["id"])] == ["failed", "skipped", "skipped"]


def test_queue_entry_failure_fails_step(env):
    eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    tick(env)
    entry = get_db().list_queue("pre")[0]
    get_db().mark_queue_failed(entry["id"], "Could not find Python binary")
    tick(env)
    assert step(1, 0)["status"] == "failed"
    assert "Python" in step(1, 0)["error"]


def test_not_live_within_grace_stays_running(env):
    eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    tick(env)
    _, pk, _ = start_queued_run("pre")
    tick(env)                                     # running; not live, record still 'running'
    tick(env)
    assert step(1, 0)["status"] == "running"


def test_run_lost_after_grace(env, monkeypatch):
    eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    tick(env)
    start_queued_run("pre")
    tick(env)
    tick(env)                                     # first not-live sighting recorded
    monkeypatch.setattr(eng, "LOST_GRACE_S", 0)
    tick(env)
    assert step(1, 0)["status"] == "failed"
    assert "run lost" in step(1, 0)["error"]


def test_task_missing_after_restart_fails(env, monkeypatch):
    eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    run = get_db().get_pipeline_run("fac", 1)
    s0 = get_db().current_step_run(run["id"], 0)
    get_db().update_step_run(s0["id"], status="succeeded", run_dir="/tmp")
    get_db().update_pipeline_run(run["id"], current_step=2)
    s2 = get_db().create_step_run(run["id"], 2, "deploy", "task")
    get_db().update_step_run(s2, status="running", pid=12345)
    tick(env)
    assert "interrupted by restart" in step(1, 2)["error"]


def test_resume_reuses_earlier_outputs(env):
    eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    tick(env)
    _, pk, _ = start_queued_run("pre")
    env["live"].add(pk)
    tick(env); finish(pk); tick(env); tick(env)
    os.makedirs(step(1, 0)["run_dir"], exist_ok=True)
    entry, pk2, _ = start_queued_run("ft")
    tick(env); finish(pk2, "crashed", 1); tick(env)
    assert get_db().get_pipeline_run("fac", 1)["status"] == "failed"
    eng.resume_pipeline_run(env["pd"], env["pl"], "fac", 1)
    run = get_db().get_pipeline_run("fac", 1)
    assert run["status"] == "running" and run["current_step"] == 1 and run["error"] is None
    assert step(1, 1)["attempt"] == 2 and step(1, 1)["status"] == "pending"
    assert step(1, 2) is None                      # skipped row cleared
    tick(env)
    entry = get_db().list_queue("ft")[0]
    assert entry["env_vars"]["BEEKEEPER_STEP_PRETRAIN_DIR"] == step(1, 0)["run_dir"]


def test_resume_refused_when_artifacts_missing(env):
    eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    tick(env)
    _, pk, _ = start_queued_run("pre")
    env["live"].add(pk)
    tick(env); finish(pk); tick(env); tick(env)   # run_dir never created on disk
    _, pk2, _ = start_queued_run("ft")
    tick(env); finish(pk2, "crashed", 1); tick(env)
    with pytest.raises(eng.PipelineError) as e:
        eng.resume_pipeline_run(env["pd"], env["pl"], "fac", 1)
    assert e.value.code == "ARTIFACTS_MISSING" and "pretrain" in e.value.message


def test_resume_only_latest_and_only_failed(env):
    eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    with pytest.raises(eng.PipelineError) as e:
        eng.resume_pipeline_run(env["pd"], env["pl"], "fac", 1)
    assert e.value.code == "NOT_RESUMABLE"


def test_cancel_while_queued(env):
    eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    tick(env)
    eng.cancel_pipeline_run(env["pd"], env["pl"], "fac", 1)
    assert get_db().list_queue("pre") == []
    run = get_db().get_pipeline_run("fac", 1)
    assert run["status"] == "cancelled"
    assert [s["status"] for s in get_db().list_step_runs(run["id"])] == ["cancelled", "skipped", "skipped"]


def test_cancel_when_queue_entry_already_started_stops_run(env):
    eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    tick(env)
    _, pk, n = start_queued_run("pre")            # dispatcher won the race; step still 'queued'
    eng.cancel_pipeline_run(env["pd"], env["pl"], "fac", 1)
    assert env["stops"] == [("pre", n)]


def test_cancel_running_train_step(env):
    eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    tick(env)
    _, pk, n = start_queued_run("pre")
    env["live"].add(pk)
    tick(env)
    eng.cancel_pipeline_run(env["pd"], env["pl"], "fac", 1)
    assert env["stops"] == [("pre", n)]


def test_snapshot_isolated_from_edits(env):
    eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    store.save_pipeline(env["pl"], {"name": "fac", "steps": [{"name": "x", "type": "task", "command": "true"}]})
    tick(env)
    assert get_db().list_queue("pre")                # still runs the snapshot's step 0


def test_describe_run(env):
    eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    tick(env)
    get_db().set_queue_wait_reason(get_db().list_queue("pre")[0]["id"], "At capacity")
    d = eng.describe_run(get_db().get_pipeline_run("fac", 1))
    assert d["run_ref"] == "fac#1"
    assert [s["status"] for s in d["steps"]] == ["queued", "pending", "pending"]
    assert d["steps"][0]["wait_reason"] == "At capacity" and d["steps"][0]["project"] == "pre"


def test_started_entry_with_deleted_run_record_fails_step(env):
    eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    tick(env)
    _, pk, _ = start_queued_run("pre")
    get_db().delete_training_run(pk)              # e.g. "Clear All History" before the next tick
    tick(env)
    assert step(1, 0)["status"] == "failed"
    assert "deleted" in step(1, 0)["error"]


def _fail_step_updates(on):
    """Make every UPDATE of pipeline_step_runs abort (simulates the step write failing)."""
    import sqlite3
    conn = sqlite3.connect(get_db().db_path)
    if on:
        conn.execute("CREATE TRIGGER fail_step_update BEFORE UPDATE ON pipeline_step_runs "
                     "BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END")
    else:
        conn.execute("DROP TRIGGER fail_step_update")
    conn.commit()
    conn.close()


def test_train_launch_is_atomic_no_duplicate_enqueue(env):
    eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    _fail_step_updates(True)
    tick(env)                                     # enqueue + step write fail together
    assert get_db().list_queue("pre") == []
    assert step(1, 0)["status"] == "pending"
    _fail_step_updates(False)
    tick(env)
    tick(env)
    assert len(get_db().list_queue("pre")) == 1
    assert step(1, 0)["status"] == "queued" and step(1, 0)["queue_id"] == get_db().list_queue("pre")[0]["id"]


def _task_only_pipeline(env):
    store.save_pipeline(env["pl"], {"name": "tsk", "steps": [
        {"name": "work", "type": "task", "command": "sleep 0.3"}]})
    eng.start_pipeline_run(env["pd"], env["pl"], "tsk")
    return get_db().get_pipeline_run("tsk", 1)


def test_task_launch_not_respawned_after_failed_step_write(env, monkeypatch):
    run = _task_only_pipeline(env)
    spawns = []
    real_spawn = eng.pipeline_tasks.spawn_task
    monkeypatch.setattr(eng.pipeline_tasks, "spawn_task",
                        lambda pk, *a, **k: spawns.append(pk) or real_spawn(pk, *a, **k))
    real_update = get_db().update_step_run
    calls = {"n": 0}

    def flaky_update(pk, **fields):
        if fields.get("status") == "running" and calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("simulated write failure")
        return real_update(pk, **fields)

    monkeypatch.setattr(type(get_db()), "update_step_run", lambda self, pk, **f: flaky_update(pk, **f))
    tick(env)                                     # spawned, step write fails
    assert get_db().current_step_run(run["id"], 0)["status"] == "pending"
    tick(env)                                     # already tracked: record, don't spawn
    assert len(spawns) == 1
    s = get_db().current_step_run(run["id"], 0)
    assert s["status"] == "running" and s["log_path"]
    deadline = time.time() + 10
    while get_db().get_pipeline_run("tsk", 1)["status"] == "running" and time.time() < deadline:
        tick(env); time.sleep(0.05)
    assert get_db().get_pipeline_run("tsk", 1)["status"] == "succeeded"
    assert len(spawns) == 1


def test_resume_guards_missing_latest(env, monkeypatch):
    eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    run = get_db().get_pipeline_run("fac", 1)
    get_db().update_pipeline_run(run["id"], status="failed")
    monkeypatch.setattr(type(get_db()), "latest_pipeline_run", lambda self, name: None)
    with pytest.raises(eng.PipelineError) as e:
        eng.resume_pipeline_run(env["pd"], env["pl"], "fac", 1)
    assert e.value.code == "NOT_LATEST"


def _succeed_step0_without_advancing(env):
    eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    run = get_db().get_pipeline_run("fac", 1)
    s0 = get_db().current_step_run(run["id"], 0)
    get_db().update_step_run(s0["id"], status="succeeded", run_dir="/tmp")
    return run


def test_recovery_settles_succeeded_step_once(env):
    run = _succeed_step0_without_advancing(env)
    tick(env)                                     # settle: advance to step 1, create pending row
    tick(env)                                     # launch step 1
    tick(env)
    assert get_db().get_pipeline_run("fac", 1)["current_step"] == 1
    assert step(1, 1)["attempt"] == 1 and step(1, 1)["status"] == "queued"
    assert len(get_db().list_queue("ft")) == 1
    assert [s["step_index"] for s in get_db().list_step_runs(run["id"])] == [0, 1]


def test_cancel_after_unsettled_success_advances_then_cancels(env):
    run = _succeed_step0_without_advancing(env)
    r = eng.cancel_pipeline_run(env["pd"], env["pl"], "fac", 1)
    assert r["status"] == "cancelled"
    run = get_db().get_pipeline_run("fac", 1)
    assert run["status"] == "cancelled" and run["current_step"] == 1 and "fine-tune" in run["error"]
    assert [s["status"] for s in get_db().list_step_runs(run["id"])] == ["succeeded", "cancelled", "skipped"]


def test_cancel_after_unsettled_final_success_reports_succeeded(env):
    eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    run = get_db().get_pipeline_run("fac", 1)
    for i, s in enumerate(PIPE["steps"]):
        sr = get_db().current_step_run(run["id"], i) or {"id": get_db().create_step_run(run["id"], i, s["name"], s["type"])}
        get_db().update_step_run(sr["id"], status="succeeded", run_dir="/tmp")
    get_db().update_pipeline_run(run["id"], current_step=2)
    r = eng.cancel_pipeline_run(env["pd"], env["pl"], "fac", 1)
    assert r["status"] == "succeeded"
    assert get_db().get_pipeline_run("fac", 1)["status"] == "succeeded"


def test_cancel_with_missing_current_row_skips_rest(env):
    eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    run = get_db().get_pipeline_run("fac", 1)
    s0 = get_db().current_step_run(run["id"], 0)
    get_db().update_step_run(s0["id"], status="succeeded", run_dir="/tmp")
    get_db().update_pipeline_run(run["id"], current_step=1)          # row for step 1 never written
    eng.cancel_pipeline_run(env["pd"], env["pl"], "fac", 1)
    run = get_db().get_pipeline_run("fac", 1)
    assert run["status"] == "cancelled" and run["error"]
    assert [s["status"] for s in get_db().list_step_runs(run["id"])] == ["succeeded", "cancelled", "skipped"]


def test_cancel_with_terminal_failed_row_settles(env):
    eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    run = get_db().get_pipeline_run("fac", 1)
    s0 = get_db().current_step_run(run["id"], 0)
    get_db().update_step_run(s0["id"], status="failed", error="boom")
    r = eng.cancel_pipeline_run(env["pd"], env["pl"], "fac", 1)
    run = get_db().get_pipeline_run("fac", 1)
    assert r["status"] == run["status"] == "failed" and "boom" in run["error"]
    assert [s["status"] for s in get_db().list_step_runs(run["id"])] == ["failed", "skipped", "skipped"]


def test_cancel_pending_task_kills_untracked_spawn(env, monkeypatch):
    store.save_pipeline(env["pl"], {"name": "tsk", "steps": [
        {"name": "work", "type": "task", "command": "sleep 30"}]})
    eng.start_pipeline_run(env["pd"], env["pl"], "tsk")
    real_update = get_db().update_step_run
    original = type(get_db()).update_step_run

    def failing_running_write(self, pk, **f):
        if f.get("status") == "running":
            raise RuntimeError("simulated write failure")
        return real_update(pk, **f)

    monkeypatch.setattr(type(get_db()), "update_step_run", failing_running_write)
    tick(env)                                     # spawned, but the step stays 'pending'
    assert eng.pipeline_tasks.running_task_pks()
    monkeypatch.setattr(type(get_db()), "update_step_run", original)
    eng.cancel_pipeline_run(env["pd"], env["pl"], "tsk", 1)
    assert eng.pipeline_tasks.running_task_pks() == []
    assert get_db().get_pipeline_run("tsk", 1)["status"] == "cancelled"
