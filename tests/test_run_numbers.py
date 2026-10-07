import sqlite3

import pytest

from services.db_service import DatabaseService


def _legacy_db(path):
    """A pre-change database: metric_analyses keyed by run_id, no run_number."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE training_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name TEXT NOT NULL,
            started_at TIMESTAMP NOT NULL,
            ended_at TIMESTAMP, duration_seconds INTEGER,
            status TEXT NOT NULL, exit_code INTEGER,
            commit_sha TEXT, commit_message TEXT, branch TEXT,
            python_version TEXT, gpu_info TEXT, hostname TEXT,
            log_file_path TEXT, tensorboard_dir TEXT, persistent_dir TEXT,
            notes TEXT NOT NULL DEFAULT '', notable INTEGER NOT NULL DEFAULT 0,
            tags TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE metric_analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            metric_name TEXT NOT NULL,
            trend TEXT, recent_trend TEXT, initial_value REAL, final_value REAL,
            best_value REAL, best_step INTEGER, peak_value REAL, peak_step INTEGER,
            peak_reversal_pct REAL DEFAULT 0, improvement_percent REAL,
            converged BOOLEAN DEFAULT 0, convergence_step INTEGER,
            anomaly_count INTEGER DEFAULT 0, anomaly_details TEXT, summary TEXT,
            sampled_points TEXT, total_points INTEGER,
            smoothed_points TEXT, smoothed_final_value REAL, ema_alpha REAL,
            parsed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (run_id) REFERENCES training_runs(id) ON DELETE CASCADE,
            UNIQUE(run_id, metric_name)
        );
        CREATE INDEX idx_metric_analyses_run ON metric_analyses(run_id);
    """)
    conn.commit()
    return conn


def test_migration_renames_metric_analyses_run_id_to_run_pk(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = _legacy_db(path)
    conn.execute(
        "INSERT INTO training_runs (project_name, started_at, status) VALUES ('a', '2026-01-01T00:00:00', 'completed')"
    )
    conn.execute("INSERT INTO metric_analyses (run_id, metric_name, trend) VALUES (1, 'loss', 'improving')")
    conn.commit()
    conn.close()

    db = DatabaseService(path)

    assert db.get_metric_analyses(1)["loss"]["trend"] == "improving"
    c = sqlite3.connect(path)
    cols = {r[1] for r in c.execute("PRAGMA table_info(metric_analyses)")}
    assert "run_pk" in cols and "run_id" not in cols
    fks = [(r[2], r[3], r[4]) for r in c.execute("PRAGMA foreign_key_list(metric_analyses)")]
    assert ("training_runs", "run_pk", "id") in fks
    indexes = {r[1] for r in c.execute("PRAGMA index_list(metric_analyses)")}
    assert "idx_metric_analyses_run_pk" in indexes and "idx_metric_analyses_run" not in indexes
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO metric_analyses (run_pk, metric_name) VALUES (1, 'loss')")


def test_fresh_db_metric_analyses_uses_run_pk(tmp_path):
    path = str(tmp_path / "fresh.db")
    DatabaseService(path)
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(metric_analyses)")}
    assert "run_pk" in cols and "run_id" not in cols


import datetime
import threading

from services.run_refs import public_run, run_ref


def _meta():
    return {"started_at": datetime.datetime.now(), "status": "starting", "branch": "main"}


@pytest.fixture
def db(tmp_path):
    return DatabaseService(str(tmp_path / "t.db"))


def test_interleaved_projects_number_independently(db):
    seq = [(p, db.create_training_run(p, _meta())[1]) for p in ["a", "b", "a", "a", "b"]]
    assert seq == [("a", 1), ("b", 1), ("a", 2), ("a", 3), ("b", 2)]


def test_numbers_not_reused_after_clear(db):
    for _ in range(3):
        db.create_training_run("a", _meta())
    db.delete_all_runs("a")
    assert db.create_training_run("a", _meta())[1] == 4


def test_numbers_not_reused_after_prune(db):
    for _ in range(5):
        db.create_training_run("a", _meta())
    db.prune_old_runs("a", keep_last=1)
    assert db.create_training_run("a", _meta())[1] == 6


def test_rename_continues_sequence(db):
    db.create_training_run("a", _meta())
    db.create_training_run("a", _meta())
    db.rename_project_runs("a", "a2")
    assert db.get_run("a2", 2) is not None
    assert db.create_training_run("a2", _meta())[1] == 3


def test_delete_project_runs_restarts_numbering(db):
    pk, _ = db.create_training_run("a", _meta())
    db.save_metric_analysis(pk, "loss", {"trend": "stable"})
    db.delete_project_runs("a")
    assert db.get_metric_analyses(pk) == {}
    assert db.create_training_run("a", _meta())[1] == 1


def test_concurrent_creates_get_distinct_numbers(db):
    numbers, lock = [], threading.Lock()

    def worker():
        _, n = db.create_training_run("a", _meta())
        with lock:
            numbers.append(n)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(numbers) == list(range(1, 11))


def test_get_run_is_scoped_to_project(db):
    db.create_training_run("a", _meta())
    db.create_training_run("b", _meta())
    assert db.get_run("a", 1)["project_name"] == "a"
    assert db.get_run("b", 1)["project_name"] == "b"
    assert db.get_run("a", 2) is None


def test_public_run_hides_pk_and_adds_ref(db):
    pk, _ = db.create_training_run("mnist", _meta())
    out = public_run(db.get_run("mnist", 1))
    assert "id" not in out and "run_number" not in out
    assert out["run_id"] == 1
    assert out["run_ref"] == "mnist-1" == run_ref("mnist", 1)


def test_create_project_purges_orphan_history(app, mocker):
    from services import project_service
    from services.db_service import get_db
    db = get_db()
    db.create_training_run("ghost", _meta())  # orphan: no project directory
    db.create_training_run("ghost", _meta())
    mocker.patch.object(project_service.threading, "Thread")

    project_service.create_project(app.config["PROJECTS_DIR"], {
        "name": "ghost", "git_url": "https://github.com/user/repo.git",
    })

    assert db.get_training_runs("ghost", limit=None) == []
    assert db.create_training_run("ghost", _meta())[1] == 1


def test_backfill_numbers_each_project_by_started_at(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = _legacy_db(path)
    rows = [  # (project, started_at) inserted out of chronological order
        ("a", "2026-01-03"), ("b", "2026-01-01"), ("a", "2026-01-01"),
        ("a", "2026-01-02"), ("b", "2026-01-05"),
    ]
    for p, s in rows:
        conn.execute(
            "INSERT INTO training_runs (project_name, started_at, status) VALUES (?, ?, 'completed')", (p, s)
        )
    conn.commit()
    conn.close()

    db = DatabaseService(path)

    got = sqlite3.connect(path).execute(
        "SELECT project_name, started_at, run_number FROM training_runs ORDER BY project_name, run_number"
    ).fetchall()
    assert got == [
        ("a", "2026-01-01", 1), ("a", "2026-01-02", 2), ("a", "2026-01-03", 3),
        ("b", "2026-01-01", 1), ("b", "2026-01-05", 2),
    ]
    assert db.create_training_run("a", _meta())[1] == 4
    assert db.create_training_run("b", _meta())[1] == 3


def test_backfill_is_idempotent(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = _legacy_db(path)
    conn.execute("INSERT INTO training_runs (project_name, started_at, status) VALUES ('a', '2026-01-01', 'completed')")
    conn.commit()
    conn.close()
    DatabaseService(path)
    db = DatabaseService(path)  # second startup
    assert db.create_training_run("a", _meta())[1] == 2


def test_unique_index_rejects_duplicate_numbers(db):
    db.create_training_run("a", _meta())
    with pytest.raises(sqlite3.IntegrityError):
        with sqlite3.connect(db.db_path) as c:
            c.execute(
                "INSERT INTO training_runs (project_name, run_number, started_at, status) VALUES ('a', 1, 'x', 'x')"
            )


def test_migration_helpers_round_trip(db):
    assert db.migration_applied("x") is False
    db.mark_migration_applied("x")
    db.mark_migration_applied("x")
    assert db.migration_applied("x") is True


def test_backfill_continues_after_existing_numbers(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = _legacy_db(path)
    conn.execute("ALTER TABLE training_runs ADD COLUMN run_number INTEGER")
    conn.execute("INSERT INTO training_runs (project_name, run_number, started_at, status) VALUES ('a', 5, '2026-01-01', 'x')")
    for s in ("2026-01-02", "2026-01-03", "2026-01-04"):
        conn.execute("INSERT INTO training_runs (project_name, started_at, status) VALUES ('a', ?, 'x')", (s,))
    conn.commit()
    conn.close()
    db = DatabaseService(path)
    got = sqlite3.connect(path).execute("SELECT run_number FROM training_runs ORDER BY started_at").fetchall()
    assert got == [(5,), (6,), (7,), (8,)]
    assert db.create_training_run("a", _meta())[1] == 9
