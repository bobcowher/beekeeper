import datetime

import pytest

from services.db_service import get_db
from tests.conftest import make_project_dir


@pytest.fixture
def db(app):
    return get_db()


def test_enqueue_and_list_fifo(db):
    a = db.enqueue_run("p", branch="dev", env_vars={"LR": "1"})
    b = db.enqueue_run("p")
    db.enqueue_run("q")
    q = db.list_queue("p")
    assert [e["id"] for e in q] == [a, b]
    assert q[0]["branch"] == "dev" and q[0]["env_vars"] == {"LR": "1"} and q[0]["source"] == "manual"
    assert q[1]["env_vars"] == {} and q[1]["status"] == "queued"
    assert sorted(db.projects_with_queued_runs()) == ["p", "q"]


def test_started_entries_leave_the_queue(db):
    a = db.enqueue_run("p")
    db.mark_queue_started(a, run_pk=42)
    assert db.list_queue("p") == []
    e = db.get_queue_entry(a)
    assert e["status"] == "started" and e["run_pk"] == 42 and e["started_at"]


def test_cancel_only_when_queued(db):
    a = db.enqueue_run("p")
    assert db.cancel_queue_entry(a, error="user") is True
    assert db.cancel_queue_entry(a) is False
    assert db.get_queue_entry(a)["status"] == "cancelled"


def test_wait_reason_and_failure(db):
    a = db.enqueue_run("p")
    db.set_queue_wait_reason(a, "At capacity (2 parallel runs)")
    assert db.list_queue("p")[0]["wait_reason"].startswith("At capacity")
    db.mark_queue_failed(a, "Project not found")
    assert db.get_queue_entry(a)["status"] == "failed"
    assert db.list_queue("p") == []


def test_rename_and_cancel_project_queue(db):
    a = db.enqueue_run("old")
    db.rename_queue_project("old", "new")
    assert [e["id"] for e in db.list_queue("new")] == [a]
    db.cancel_project_queue("new", "project deleted")
    assert db.get_queue_entry(a)["status"] == "cancelled"
    assert db.get_queue_entry(a)["error"] == "project deleted"


def test_purge_keeps_queued_and_recent(db):
    old_done = db.enqueue_run("p")
    db.mark_queue_failed(old_done, "x")
    old_queued = db.enqueue_run("p")
    recent_done = db.enqueue_run("p")
    db.mark_queue_failed(recent_done, "x")
    stale = (datetime.datetime.now() - datetime.timedelta(days=31)).isoformat()
    with db._get_connection() as conn:
        conn.execute("UPDATE run_queue SET created_at=? WHERE id IN (?, ?)", (stale, old_done, old_queued))
        conn.commit()
    assert db.purge_old_queue_entries(days=30) == 1
    assert db.get_queue_entry(old_done) is None
    assert db.get_queue_entry(old_queued) is not None
    assert db.get_queue_entry(recent_done) is not None


def test_api_queue_roundtrip(client, app):
    make_project_dir(app, "p")
    r = client.post("/api/v1/projects/p/training/start",
                    json={"queue": True, "branch": "dev", "env_vars": {"LR": "1"}})
    assert r.status_code == 202
    data = r.get_json()["data"]
    assert data["status"] == "queued" and data["position"] == 1
    listing = client.get("/api/v1/projects/p/queue").get_json()["data"]["queue"]
    assert listing == [{
        "queue_id": data["queue_id"], "position": 1, "branch": "dev", "env_vars": {"LR": "1"},
        "source": "manual", "wait_reason": None, "created_at": listing[0]["created_at"],
    }]
    assert client.delete(f"/api/v1/projects/p/queue/{data['queue_id']}").status_code == 200
    assert client.delete(f"/api/v1/projects/p/queue/{data['queue_id']}").status_code == 409


def test_api_queue_rejects_bad_env(client, app):
    make_project_dir(app, "p")
    r = client.post("/api/v1/projects/p/training/start", json={"queue": True, "env_vars": {"A": 1}})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "INVALID_ENV_VARS"


def test_api_queue_unknown_project_404(client):
    assert client.post("/api/v1/projects/ghost/training/start", json={"queue": True}).status_code == 404


def test_api_cancel_other_projects_entry_404(client, app):
    make_project_dir(app, "p")
    make_project_dir(app, "q")
    qid = get_db().enqueue_run("q")
    assert client.delete(f"/api/v1/projects/p/queue/{qid}").status_code == 404


def test_rename_moves_queue(app):
    from services.project_service import rename_project
    make_project_dir(app, "old")
    qid = get_db().enqueue_run("old")
    rename_project(app.config["PROJECTS_DIR"], "old", "new")
    assert get_db().get_queue_entry(qid)["project_name"] == "new"


def test_delete_cancels_queue(app):
    from services.project_service import delete_project
    make_project_dir(app, "p")
    qid = get_db().enqueue_run("p")
    delete_project(app.config["PROJECTS_DIR"], "p")
    assert get_db().get_queue_entry(qid)["status"] == "cancelled"
