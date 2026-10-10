# tests/test_pipeline_store.py
import os

import pytest

from services import pipeline_store as store
from services.db_service import get_db
from services.pipeline_schema import PipelineValidationError

TEXT = "# my pipeline\nname: pl\nsteps:\n  - name: a\n    type: task\n    command: echo hi\n"
DEFN = {"name": "pl", "folder": "", "description": "",
        "steps": [{"name": "a", "type": "task", "command": "echo hi", "timeout_s": 3600},
                  {"name": "b", "type": "train", "project": "p", "env_vars": {}}]}


@pytest.fixture
def pdir(app):
    return app.config["PIPELINES_DIR"]


def test_save_text_preserves_comments_and_loads(pdir):
    store.save_pipeline_text(pdir, "pl", TEXT)
    defn, raw = store.load_pipeline(pdir, "pl")
    assert raw.startswith("# my pipeline") and defn["steps"][0]["command"] == "echo hi"


def test_save_text_rejects_name_mismatch(pdir):
    with pytest.raises(PipelineValidationError):
        store.save_pipeline_text(pdir, "other", TEXT)
    assert not store.pipeline_exists(pdir, "other")


def test_save_dict_and_list(pdir):
    store.save_pipeline(pdir, DEFN)
    os.makedirs(os.path.join(pdir, "broken"))
    open(os.path.join(pdir, "broken", "pipeline.yaml"), "w").write("name: [")
    listing = {p["name"]: p for p in store.list_pipelines(pdir)}
    assert listing["pl"]["steps"][1]["project"] == "p"
    assert "error" in listing["broken"]


def test_load_missing_raises(pdir):
    with pytest.raises(FileNotFoundError):
        store.load_pipeline(pdir, "nope")


def test_run_dir_is_absolute(pdir):
    path = store.pipeline_run_dir(pdir, "pl", 3)
    assert os.path.isabs(path) and path.endswith(os.path.join("pl", "runs", "run_3"))


def test_pipeline_run_numbers_and_single_active(app):
    db = get_db()
    pk1, n1 = db.create_pipeline_run("pl", DEFN)
    assert n1 == 1
    assert db.create_pipeline_run("pl", DEFN) is None          # already running
    db.update_pipeline_run(pk1, status="failed")
    pk2, n2 = db.create_pipeline_run("pl", DEFN)
    assert n2 == 2
    run = db.get_pipeline_run("pl", 2)
    assert run["definition"] == DEFN and run["status"] == "running" and run["current_step"] == 0
    assert db.current_step_run(pk2, 0)["status"] == "pending"
    assert [r["run_number"] for r in db.running_pipeline_runs()] == [2]
    assert db.latest_pipeline_run("pl")["id"] == pk2


def test_step_attempts_and_listing(app):
    db = get_db()
    pk, _ = db.create_pipeline_run("pl", DEFN)
    s0 = db.current_step_run(pk, 0)
    db.update_step_run(s0["id"], status="failed", error="boom")
    db.create_step_run(pk, 1, "b", "train", status="skipped")
    db.delete_skipped_steps(pk)
    retry = db.create_step_run(pk, 0, "a", "task")
    rows = db.list_step_runs(pk)
    assert [(r["step_index"], r["attempt"], r["status"]) for r in rows] == [(0, 2, "pending")]
    assert rows[0]["id"] == retry


def test_delete_pipeline_records_resets_numbering(app):
    db = get_db()
    pk, _ = db.create_pipeline_run("pl", DEFN)
    db.update_pipeline_run(pk, status="succeeded")
    db.delete_pipeline_records("pl")
    assert db.latest_pipeline_run("pl") is None
    assert db.create_pipeline_run("pl", DEFN)[1] == 1
