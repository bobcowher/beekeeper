"""Pipeline engine: inherit, per-step scripts and project tasks (spec 2026-10-17)."""
import datetime
import json
import os
import time

import pytest

from services import pipeline_engine as eng
from services import pipeline_store as store
from services.db_service import get_db
from services.process_manager import TASK_TIMEOUT_EXIT
from tests.conftest import make_project_dir

PIPE = {"name": "fac", "steps": [
    {"name": "pretrain", "type": "train", "project": "m", "script": "pretrain.py"},
    {"name": "check", "type": "task", "project": "m", "command": "test -s checkpoints/p.pt",
     "inherit": ["checkpoints"], "timeout_s": 30},
    {"name": "posttrain", "type": "train", "project": "m", "script": "posttrain.py",
     "inherit": ["checkpoints"]},
    {"name": "export", "type": "task", "inherit": ["checkpoints"],
     "command": "test -s checkpoints/p.pt && echo exported > checkpoints/exported.txt"},
]}


@pytest.fixture
def env(app, monkeypatch):
    make_project_dir(app, "m", output_paths=["checkpoints"])
    store.save_pipeline(app.config["PIPELINES_DIR"], PIPE, projects_dir=app.config["PROJECTS_DIR"])
    live = set()
    monkeypatch.setattr(eng.process_manager, "is_run_live", lambda pk: pk in live)
    stops = []
    monkeypatch.setattr(eng.process_manager, "stop_training",
                        lambda d, name, run_id=None: stops.append((name, run_id)) or {"status": "stopped"})
    return {"pd": app.config["PROJECTS_DIR"], "pl": app.config["PIPELINES_DIR"], "live": live, "stops": stops,
            "app": app}


def tick(env):
    eng.tick(env["pd"], env["pl"])


def start_queued(env, project="m", files=("checkpoints/p.pt",)):
    """Simulate the dispatcher starting the head queue entry; give the run folder some files."""
    db = get_db()
    entry = db.list_queue(project)[0]
    pk, n = db.create_training_run(project, {"started_at": datetime.datetime.now(), "status": "running",
                                             "kind": entry["kind"]})
    db.mark_queue_started(entry["id"], pk)
    run_dir = os.path.join(env["pd"], project, "persistent", "runs", f"run_{n}")
    for rel in files:
        os.makedirs(os.path.dirname(os.path.join(run_dir, rel)), exist_ok=True)
        with open(os.path.join(run_dir, rel), "w") as f:
            f.write("weights")
    env["live"].add(pk)
    return entry, pk, n, run_dir


def finish(env, pk, status="completed", code=0):
    get_db().update_training_run(pk, status=status, exit_code=code)
    env["live"].discard(pk)


def step(idx, n=1):
    return get_db().current_step_run(get_db().get_pipeline_run("fac", n)["id"], idx)


def run_step0(env):
    eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    tick(env)
    entry, pk, n, run_dir = start_queued(env)
    tick(env)
    return entry, pk, n, run_dir


def test_train_step_script_reaches_the_queue(env):
    entry, *_ = run_step0(env)
    assert entry["script"] == "pretrain.py" and entry["kind"] == "train" and entry["inherit"] == []
    assert entry["env_vars"]["BEEKEEPER_PIPELINE_RUN_ID"] == "1"


def test_project_task_enqueued_with_kind_task_and_inherit_from_previous_step(env):
    _, pk, n, run_dir = run_step0(env)
    finish(env, pk)
    tick(env)
    entry = get_db().list_queue("m")[0]
    assert entry["kind"] == "task" and entry["command"] == "test -s checkpoints/p.pt"
    assert entry["timeout_s"] == 30 and entry["inherit"] == ["checkpoints"]
    assert entry["source_run_dir"] == run_dir
    assert entry["source_step"] == "pretrain" and entry["source_run_ref"] == f"m-{n}"


def test_r5_no_step_dir_variables_when_inheriting(env):
    _, pk, _, _ = run_step0(env)
    finish(env, pk)
    tick(env)
    env_vars = get_db().list_queue("m")[0]["env_vars"]
    assert not [k for k in env_vars if k.startswith("BEEKEEPER_STEP_") or k == "BEEKEEPER_PREV_STEP_DIR"]
    assert env_vars["BEEKEEPER_PIPELINE"] == "fac" and "BEEKEEPER_PIPELINE_RUN_DIR" in env_vars


def test_missing_inherited_path_fails_before_queueing(env):
    eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    tick(env)
    _, pk, n, _ = start_queued(env, files=("other.txt",))
    tick(env)
    finish(env, pk)
    tick(env)
    assert get_db().list_queue("m") == []
    assert step(1)["status"] == "failed"
    assert step(1)["error"] == f"inherit: checkpoints not found in m-{n}'s run folder"


def test_launch_repeats_project_checks(env):
    _, pk, _, _ = run_step0(env)
    make_project_dir(env["app"], "m", output_paths=[])   # changed after the pipeline was saved
    finish(env, pk)
    tick(env)
    assert step(1)["status"] == "failed"
    assert "steps[1].inherit: 'checkpoints' isn't an output path of the previous step (pretrain)" in step(1)["error"]
    assert get_db().list_queue("m") == []


def _through_check(env):
    _, pk, _, run_dir0 = run_step0(env)
    finish(env, pk)
    tick(env)                                    # check queued
    _, pk1, n1, run_dir1 = start_queued(env)
    tick(env)                                    # check running
    return pk1, n1, run_dir1


def test_project_task_runs_as_a_project_run_and_succeeds(env):
    pk1, n1, run_dir1 = _through_check(env)
    assert step(1)["status"] == "running" and step(1)["run_pk"] == pk1 and step(1)["run_dir"] == run_dir1
    view = eng.describe_run(get_db().get_pipeline_run("fac", 1))["steps"][1]
    assert view["project"] == "m" and view["run_ref"] == f"m-{n1}"
    finish(env, pk1)
    tick(env)
    assert step(1)["status"] == "succeeded"
    entry = get_db().list_queue("m")[0]       # posttrain inherits from the check task's run
    assert entry["source_run_dir"] == run_dir1 and entry["script"] == "posttrain.py"


def test_project_task_timeout_reported(env):
    pk1, _, _ = _through_check(env)
    finish(env, pk1, status="crashed", code=TASK_TIMEOUT_EXIT)
    tick(env)
    assert step(1)["status"] == "failed" and step(1)["error"] == "timed out after 30s"


def test_project_task_crash_reported(env):
    pk1, _, _ = _through_check(env)
    finish(env, pk1, status="crashed", code=2)
    tick(env)
    assert step(1)["error"] == "task run crashed"


def test_cancel_running_project_task_stops_its_run(env):
    pk1, n1, _ = _through_check(env)
    eng.cancel_pipeline_run(env["pd"], env["pl"], "fac", 1)
    assert env["stops"] == [("m", n1)]


def test_plain_task_with_inherit_runs_in_its_own_step_folder(env):
    pk1, _, _ = _through_check(env)
    finish(env, pk1)
    tick(env)
    _, pk2, n2, run_dir2 = start_queued(env)
    tick(env)
    finish(env, pk2)
    deadline = time.time() + 10
    while get_db().get_pipeline_run("fac", 1)["status"] == "running" and time.time() < deadline:
        tick(env)
        time.sleep(0.05)
    assert get_db().get_pipeline_run("fac", 1)["status"] == "succeeded", step(3)
    folder = os.path.join(env["pl"], "fac", "runs", "run_1", "steps", "export")
    assert step(3)["run_dir"] == folder
    assert open(os.path.join(folder, "checkpoints", "exported.txt")).read().strip() == "exported"
    assert open(os.path.join(run_dir2, "checkpoints", "p.pt")).read() == "weights"   # source untouched
    log = open(os.path.join(env["pl"], step(3)["log_path"])).read()
    assert f"[beekeeper] Inherited checkpoints/ from posttrain (m-{n2})" in log
    view = eng.describe_run(get_db().get_pipeline_run("fac", 1))["steps"][3]
    assert view["inherited"] == [{"path": "checkpoints", "source_step": "posttrain",
                                  "source_run_ref": f"m-{n2}", "files": 1, "bytes": 7}]


def test_plain_task_without_inherit_keeps_running_in_the_pipeline_run_folder(app, env):
    store.save_pipeline(env["pl"], {"name": "plain", "steps": [
        {"name": "a", "type": "task", "command": "mkdir -p models && touch models/x"}]})
    eng.start_pipeline_run(env["pd"], env["pl"], "plain")
    deadline = time.time() + 10
    while get_db().get_pipeline_run("plain", 1)["status"] == "running" and time.time() < deadline:
        tick(env)
        time.sleep(0.05)
    run_dir = os.path.join(env["pl"], "plain", "runs", "run_1")
    assert get_db().current_step_run(get_db().get_pipeline_run("plain", 1)["id"], 0)["run_dir"] == run_dir
    assert os.path.isfile(os.path.join(run_dir, "models", "x"))


def test_rerun_from_step_inherits_from_the_live_attempt(env):
    pk1, n1, run_dir1 = _through_check(env)
    finish(env, pk1)
    tick(env)                                     # posttrain queued from the check run
    _, pk2, _, _ = start_queued(env)
    tick(env)
    finish(env, pk2, status="crashed", code=1)
    tick(env)
    assert get_db().get_pipeline_run("fac", 1)["status"] == "failed"

    eng.resume_pipeline_run(env["pd"], env["pl"], "fac", 1, from_step="check")
    tick(env)
    _, pk3, n3, run_dir3 = start_queued(env)     # check, attempt 2
    tick(env)
    finish(env, pk3)
    tick(env)
    entry = get_db().list_queue("m")[0]
    assert entry["source_run_dir"] == run_dir3 and entry["source_run_ref"] == f"m-{n3}"
    assert run_dir3 != run_dir1


def test_resume_inherits_from_the_previous_steps_existing_attempt(env):
    pk1, n1, run_dir1 = _through_check(env)
    finish(env, pk1)
    tick(env)
    _, pk2, _, _ = start_queued(env)
    tick(env)
    finish(env, pk2, status="crashed", code=1)
    tick(env)
    eng.resume_pipeline_run(env["pd"], env["pl"], "fac", 1)
    tick(env)
    assert get_db().list_queue("m")[0]["source_run_dir"] == run_dir1


def test_start_refused_when_project_settings_break_an_inherit(env):
    make_project_dir(env["app"], "m", output_paths=[])
    with pytest.raises(eng.PipelineError) as exc:
        eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    assert exc.value.code == "INHERIT_PATH_NOT_DECLARED"
    assert "steps[1].inherit" in exc.value.message


def test_missing_task_project_refused(env):
    store.save_pipeline(env["pl"], {"name": "t", "steps": [
        {"name": "a", "type": "task", "project": "ghost", "command": "true"}]})
    with pytest.raises(eng.PipelineError) as exc:
        eng.start_pipeline_run(env["pd"], env["pl"], "t")
    assert exc.value.code == "PROJECT_NOT_FOUND"


def test_project_task_refused_when_inline_tasks_disabled(env, monkeypatch):
    monkeypatch.setattr(eng, "_tasks_enabled", lambda: False)
    with pytest.raises(eng.PipelineError) as exc:
        eng.start_pipeline_run(env["pd"], env["pl"], "fac")
    assert exc.value.code == "TASKS_DISABLED"


def test_manifest_shown_for_project_step(env):
    pk1, n1, run_dir1 = _through_check(env)
    os.makedirs(os.path.join(run_dir1, ".beekeeper"))
    with open(os.path.join(run_dir1, ".beekeeper", "inherited.json"), "w") as f:
        json.dump({"inherited": [{"path": "checkpoints", "source_step": "pretrain", "source_run_ref": "m-1",
                                  "files": 2, "bytes": 10}]}, f)
    steps = eng.describe_run(get_db().get_pipeline_run("fac", 1))["steps"]
    assert steps[1]["inherited"][0]["source_run_ref"] == "m-1"
    assert steps[1]["inherit"] == ["checkpoints"]
    assert steps[0]["script"] == "pretrain.py" and "inherited" not in steps[0]
    assert steps[2]["inherited"] == []     # not reached yet
