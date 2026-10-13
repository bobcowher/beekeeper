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


def test_atomic_write_cleanup_does_not_mask_write_error(pdir, monkeypatch):
    def write_error(*args, **kwargs):
        raise OSError("write failed")

    def cleanup_error(*args, **kwargs):
        raise OSError("cleanup failed")

    monkeypatch.setattr(store.os, "replace", write_error)
    monkeypatch.setattr(store.os, "unlink", cleanup_error)
    with pytest.raises(OSError, match="write failed"):
        store.save_pipeline_text(pdir, "pl", TEXT)


def test_save_dict_and_list(pdir):
    store.save_pipeline(pdir, DEFN)
    os.makedirs(os.path.join(pdir, "broken"))
    with open(os.path.join(pdir, "broken", "pipeline.yaml"), "w") as f:
        f.write("name: [")
    listing = {p["name"]: p for p in store.list_pipelines(pdir)}
    assert listing["pl"]["steps"][1]["project"] == "p"
    assert "error" in listing["broken"]


def test_load_missing_raises(pdir):
    with pytest.raises(FileNotFoundError):
        store.load_pipeline(pdir, "nope")


def test_run_dir_is_absolute(pdir):
    path = store.pipeline_run_dir(pdir, "pl", 3)
    assert os.path.isabs(path) and path.endswith(os.path.join("pl", "runs", "run_3"))


def test_pipeline_run_numbers_and_queue(app):
    db = get_db()
    pk1, n1, status1 = db.create_pipeline_run("pl", DEFN, {"b": "feat/x"})
    assert (n1, status1) == (1, "running")
    pk2, n2, status2 = db.create_pipeline_run("pl", DEFN)
    assert (n2, status2) == (2, "queued")
    assert db.current_step_run(pk2, 0) is None
    assert db.get_pipeline_run("pl", 1)["overrides"] == {"b": "feat/x"}
    assert db.get_pipeline_run("pl", 2)["overrides"] == {}
    assert [r["run_number"] for r in db.queued_pipeline_runs("pl")] == [2]
    assert db.pipelines_with_queued_runs() == ["pl"]
    assert db.has_active_pipeline_run("pl")
    db.update_pipeline_run(pk1, status="failed")
    run = db.promote_pipeline_run("pl")
    assert run["id"] == pk2 and run["status"] == "running"
    assert run["definition"] == DEFN and run["status"] == "running" and run["current_step"] == 0
    assert db.current_step_run(pk2, 0)["status"] == "pending"
    assert [r["run_number"] for r in db.running_pipeline_runs()] == [2]
    assert db.latest_pipeline_run("pl")["id"] == pk2


def test_pipeline_queue_promotes_in_order(app):
    db = get_db()
    pk1, _, _ = db.create_pipeline_run("pl", DEFN)
    pk2, _, _ = db.create_pipeline_run("pl", DEFN)
    pk3, _, _ = db.create_pipeline_run("pl", DEFN)
    db.update_pipeline_run(pk1, status="succeeded")
    assert db.promote_pipeline_run("pl")["id"] == pk2
    assert db.promote_pipeline_run("pl") is None
    db.update_pipeline_run(pk2, status="failed")
    assert db.promote_pipeline_run("pl")["id"] == pk3


def test_latest_pipeline_run_ignores_never_started_runs(app):
    db = get_db()
    pk1, _, _ = db.create_pipeline_run("pl", DEFN)
    pk2, _, _ = db.create_pipeline_run("pl", DEFN)
    assert db.latest_pipeline_run("pl")["id"] == pk1
    db.update_pipeline_run(pk1, status="failed")
    assert db.latest_pipeline_run("pl")["id"] == pk1
    db.update_pipeline_run(pk2, status="cancelled")
    assert db.latest_pipeline_run("pl")["id"] == pk1


def test_recent_finished_pipeline_runs_excludes_queued(app):
    db = get_db()
    pk1, _, _ = db.create_pipeline_run("pl", DEFN)
    db.create_pipeline_run("pl", DEFN)
    db.update_pipeline_run(pk1, status="failed")
    assert [r["id"] for r in db.recent_finished_pipeline_runs()] == [pk1]


def test_recent_finished_pipeline_runs_skips_runs_cancelled_in_queue(app):
    db = get_db()
    pk1, _, _ = db.create_pipeline_run("pl", DEFN)
    pk2, _, _ = db.create_pipeline_run("pl", DEFN)
    db.update_pipeline_run(pk2, status="cancelled")
    db.update_pipeline_run(pk1, status="succeeded")
    assert [r["id"] for r in db.recent_finished_pipeline_runs()] == [pk1]


def test_step_attempts_and_listing(app):
    db = get_db()
    pk, _, _ = db.create_pipeline_run("pl", DEFN)
    s0 = db.current_step_run(pk, 0)
    db.update_step_run(s0["id"], status="failed", error="boom")
    db.create_step_run(pk, 1, "b", "train", status="skipped")
    db.delete_skipped_steps(pk)
    retry = db.create_step_run(pk, 0, "a", "task")
    rows = db.list_step_runs(pk)
    assert [(r["step_index"], r["attempt"], r["status"]) for r in rows] == [(0, 2, "pending")]
    assert rows[0]["id"] == retry


def test_internal_updates_require_at_least_one_field(app):
    db = get_db()
    pk, _, _ = db.create_pipeline_run("pl", DEFN)
    step = db.current_step_run(pk, 0)
    with pytest.raises(ValueError, match="at least one pipeline run field"):
        db.update_pipeline_run(pk)
    with pytest.raises(ValueError, match="at least one step run field"):
        db.update_step_run(step["id"])


def test_delete_pipeline_records_resets_numbering(app):
    db = get_db()
    pk, _, _ = db.create_pipeline_run("pl", DEFN)
    db.update_pipeline_run(pk, status="succeeded")
    db.delete_pipeline_records("pl")
    assert db.latest_pipeline_run("pl") is None
    assert db.create_pipeline_run("pl", DEFN)[1] == 1


@pytest.mark.parametrize("bad", ["../x", "", "pl\n", None, 5])
def test_run_dir_rejects_bad_names(pdir, bad):
    with pytest.raises(FileNotFoundError):
        store.pipeline_run_dir(pdir, bad, 1)


def test_bad_names_not_found(pdir):
    assert store.pipeline_exists(pdir, "../x") is False
    store.save_pipeline_text(pdir, "pl", TEXT)
    with pytest.raises(FileNotFoundError):
        store.load_pipeline(pdir, "pl\n")
