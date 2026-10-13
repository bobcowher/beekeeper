import datetime

from services.db_service import get_db

DEFN = {"name": "fac", "folder": "", "description": "",
        "steps": [{"name": "a", "type": "train", "project": "p", "env_vars": {}}]}


def make_runs(n):
    db = get_db()
    base = datetime.datetime(2026, 1, 1)
    return [db.create_training_run("p", {"started_at": base + datetime.timedelta(minutes=i), "status": "completed"})[0]
            for i in range(n)]


def attach(pk_run, status):
    db = get_db()
    created = db.create_pipeline_run("fac", DEFN)
    ppk, _, _ = created
    s = db.current_step_run(ppk, 0)
    db.update_step_run(s["id"], status="succeeded", run_pk=pk_run)
    db.update_pipeline_run(ppk, status=status)
    return ppk


def test_running_pipeline_pins_run(app):
    pks = make_runs(5)
    attach(pks[0], "running")
    deleted = {r["id"] for r in get_db().prune_old_runs("p", keep_last=2)}
    assert pks[0] not in deleted and deleted == set(pks[1:3])


def test_latest_failed_pins_but_older_failed_does_not(app):
    pks = make_runs(5)
    attach(pks[0], "failed")
    attach(pks[1], "failed")      # latest
    deleted = {r["id"] for r in get_db().prune_old_runs("p", keep_last=1)}
    assert pks[1] not in deleted and pks[0] in deleted


def test_succeeded_pipeline_does_not_pin(app):
    pks = make_runs(3)
    attach(pks[0], "succeeded")
    deleted = {r["id"] for r in get_db().prune_old_runs("p", keep_last=1)}
    assert pks[0] in deleted
