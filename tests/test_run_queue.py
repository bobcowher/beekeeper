import datetime

import pytest

from services.db_service import get_db


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
