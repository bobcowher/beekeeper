import datetime

import pytest
from flask import Flask

from app import _should_start_dispatcher
from services import dispatcher
from services.db_service import get_db
from tests.conftest import make_project_dir


@pytest.fixture
def starts(monkeypatch):
    """Fake start_training: pops scripted results; records calls."""
    state = {"calls": [], "results": []}

    def fake(projects_dir, name, branch=None, extra_env=None):
        state["calls"].append((name, branch, extra_env))
        result = state["results"].pop(0) if state["results"] else {"run_id": 1, "run_ref": f"{name}-1", "status": "starting"}
        if "error" not in result:
            get_db().create_training_run(name, {"started_at": datetime.datetime.now(), "status": "starting"})
        return result

    monkeypatch.setattr(dispatcher.process_manager, "start_training", fake)
    return state


def tick(app):
    dispatcher.tick(app.config["PROJECTS_DIR"], app.config["PIPELINES_DIR"])


def test_starts_head_of_queue_with_branch_and_env(app, starts):
    make_project_dir(app, "p")
    db = get_db()
    a = db.enqueue_run("p", branch="dev", env_vars={"LR": "1"})
    db.enqueue_run("p")
    tick(app)
    assert starts["calls"] == [("p", "dev", {"LR": "1"})]
    entry = db.get_queue_entry(a)
    assert entry["status"] == "started"
    assert entry["run_pk"] == db.get_run("p", 1)["id"]


def _step_pk(db):
    pk, _, _ = db.create_pipeline_run(
        "pl", {"name": "pl", "steps": [{"name": "s", "type": "train", "project": "p"}]}, {})
    return db.create_step_run(pk, 0, "s", "train")


def test_step_script_and_inherit_reach_start_training(app, monkeypatch):
    make_project_dir(app, "p")
    db = get_db()
    calls = []

    def fake(projects_dir, name, branch=None, extra_env=None, **step):
        calls.append(step)
        db.create_training_run(name, {"started_at": datetime.datetime.now(), "status": "starting"})
        return {"run_id": 1, "run_ref": "p-1", "status": "starting"}

    monkeypatch.setattr(dispatcher.process_manager, "start_training", fake)
    db.enqueue_step_run(_step_pk(db), "p", None, None, "pipeline:pl#1", launch={
        "script": "post.py", "inherit": ["ckpt"], "source_run_dir": "/r/run_1",
        "source_step": "pre", "source_run_ref": "p-0"})
    tick(app)
    assert calls == [{"script": "post.py", "inherit": {
        "paths": ["ckpt"], "source_run_dir": "/r/run_1", "source_step": "pre", "source_run_ref": "p-0"}}]


def test_task_entries_go_to_start_task(app, monkeypatch):
    make_project_dir(app, "p")
    db = get_db()
    calls = []

    def fake_task(projects_dir, name, command, timeout_s, inherit=None, extra_env=None):
        calls.append((name, command, timeout_s, inherit, extra_env))
        db.create_training_run(name, {"started_at": datetime.datetime.now(), "status": "starting"})
        return {"run_id": 1, "run_ref": "p-1", "status": "starting"}

    monkeypatch.setattr(dispatcher.process_manager, "start_task", fake_task)
    monkeypatch.setattr(dispatcher.process_manager, "start_training",
                        lambda *a, **k: pytest.fail("a task must not start training"))
    qid = db.enqueue_step_run(_step_pk(db), "p", None, {"A": "1"}, "pipeline:pl#1",
                              launch={"kind": "task", "command": "make check", "timeout_s": 30})
    tick(app)
    assert calls == [("p", "make check", 30, None, {"A": "1"})]
    assert db.get_queue_entry(qid)["status"] == "started"


def test_one_start_per_project_per_tick(app, starts):
    make_project_dir(app, "p")
    get_db().enqueue_run("p")
    get_db().enqueue_run("p")
    tick(app)
    assert len(starts["calls"]) == 1
    assert len(get_db().list_queue("p")) == 1


def test_transient_refusal_keeps_head_and_records_reason(app, starts):
    make_project_dir(app, "p")
    a = get_db().enqueue_run("p")
    starts["results"] = [{"error": "At capacity (2 parallel runs)", "code": "AT_CAPACITY"}]
    tick(app)
    q = get_db().list_queue("p")
    assert q[0]["id"] == a and q[0]["wait_reason"] == "At capacity (2 parallel runs)"


def test_permanent_refusal_fails_entry_and_next_tick_moves_on(app, starts):
    make_project_dir(app, "p")
    a = get_db().enqueue_run("p")
    b = get_db().enqueue_run("p", branch="other")
    starts["results"] = [{"error": "Could not find Python binary", "code": "NO_PYTHON"}]
    tick(app)
    assert get_db().get_queue_entry(a)["status"] == "failed"
    tick(app)
    assert get_db().get_queue_entry(b)["status"] == "started"


def test_setup_incomplete_is_transient_only_while_setup_active(app, starts):
    make_project_dir(app, "busy", setup_status="installing_deps")
    make_project_dir(app, "broken", setup_status="error")
    a = get_db().enqueue_run("busy")
    b = get_db().enqueue_run("broken")
    starts["results"] = [{"error": "Project setup is not complete", "code": "SETUP_INCOMPLETE"}] * 2
    tick(app)
    assert get_db().get_queue_entry(a)["status"] == "queued"
    assert get_db().get_queue_entry(b)["status"] == "failed"


def test_one_bad_project_does_not_stop_others(app, starts, monkeypatch):
    make_project_dir(app, "a")
    make_project_dir(app, "b")
    get_db().enqueue_run("a")
    get_db().enqueue_run("b")
    real = dispatcher.dispatch_project_queue

    def flaky(projects_dir, name):
        if name == "a":
            raise RuntimeError("boom")
        return real(projects_dir, name)

    monkeypatch.setattr(dispatcher, "dispatch_project_queue", flaky)
    tick(app)
    assert get_db().list_queue("b") == []


def test_dispatcher_not_started_in_testing(app):
    assert dispatcher._thread is None


def test_dispatcher_starts_only_in_debug_reloader_child(monkeypatch):
    application = Flask(__name__)
    application.debug = True

    monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)
    assert not _should_start_dispatcher(application)

    monkeypatch.setenv("WERKZEUG_RUN_MAIN", "true")
    assert _should_start_dispatcher(application)

    application.debug = False
    monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)
    assert _should_start_dispatcher(application)

    application.config["TESTING"] = True
    assert not _should_start_dispatcher(application)


def test_tick_advances_pipelines(app, monkeypatch):
    seen = []
    from services import pipeline_engine
    monkeypatch.setattr(pipeline_engine, "tick", lambda pd, pl: seen.append(pl))
    tick(app)
    assert seen == [app.config["PIPELINES_DIR"]]


def test_cancel_between_start_and_mark_stops_run(app, starts, monkeypatch):
    """Review Focus 2: a cancel lands after start_training but before mark_queue_started."""
    make_project_dir(app, "p")
    db = get_db()
    qid = db.enqueue_run("p", source="pipeline:fac#1")
    real_fake = dispatcher.process_manager.start_training

    def start_then_cancel(projects_dir, name, branch=None, extra_env=None):
        result = real_fake(projects_dir, name, branch=branch, extra_env=extra_env)
        assert db.cancel_queue_entry(qid, error="pipeline cancelled")
        return result

    monkeypatch.setattr(dispatcher.process_manager, "start_training", start_then_cancel)
    stops = []
    monkeypatch.setattr(dispatcher.process_manager, "stop_training",
                        lambda d, name, run_id=None: stops.append((name, run_id)) or {"status": "stopped"})
    tick(app)
    assert stops == [("p", 1)]
    entry = db.get_queue_entry(qid)
    assert entry["status"] == "cancelled" and entry["run_pk"] is None


def test_started_run_is_stopped_when_queue_handoff_fails(app, starts, monkeypatch):
    """A DB error after start_training must not leave queued work able to launch twice."""
    make_project_dir(app, "p")
    db = get_db()
    qid = db.enqueue_run("p")

    def fail_lookup(name, run_id):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db, "get_run", fail_lookup)
    stops = []
    monkeypatch.setattr(dispatcher.process_manager, "stop_training",
                        lambda d, name, run_id=None: stops.append((name, run_id)) or {"status": "stopped"})

    with pytest.raises(RuntimeError, match="database unavailable"):
        dispatcher.dispatch_project_queue(app.config["PROJECTS_DIR"], "p")

    assert stops == [("p", 1)]
    assert db.get_queue_entry(qid)["status"] == "queued"
