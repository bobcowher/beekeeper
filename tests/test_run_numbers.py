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


# --- persistent folder migration -------------------------------------------

import os

from services import run_folder_migration as rfm


def _reset_folder_migration():
    from services.db_service import get_db
    with get_db()._get_connection() as c:
        c.execute("DELETE FROM schema_migrations")
        c.commit()


def _mk_run(db, projects_dir, project, pk_dir=True):
    pk, n = db.create_training_run(project, _meta())
    rel = f"persistent/runs/run_{pk}"
    db.update_training_run(pk, persistent_dir=rel, tensorboard_dir=rel)
    if pk_dir:
        os.makedirs(os.path.join(projects_dir, project, rel))
        with open(os.path.join(projects_dir, project, rel, "marker.txt"), "w") as f:
            f.write(f"pk={pk}")
    return pk, n


def test_folder_migration_renames_to_numbers_and_handles_collisions(app, mocker):
    from services.db_service import get_db
    _reset_folder_migration()
    db = get_db()
    projects_dir = app.config["PROJECTS_DIR"]
    mocker.patch.object(rfm, "_live_run_dirs", return_value=[])

    _mk_run(db, projects_dir, "z", pk_dir=False)            # pk 1 (z #1), shifts pks
    projects = ["a", "b", "a", "b", "a"]
    runs = [_mk_run(db, projects_dir, p) for p in projects]
    # a: pks 2,4,6 -> #1,#2,#3   b: pks 3,5 -> #1,#2
    # Collision: a's old folder run_2 (pk 2, becoming run_1) holds the name that a's pk-4
    # run (#2) needs. Marker files prove no folder was clobbered or swapped.

    assert rfm.migrate_run_folders(projects_dir) == "done"

    for (pk, n), project in zip(runs, projects):
        row = db.get_run(project, n)
        assert row["persistent_dir"] == f"persistent/runs/run_{n}"
        assert row["tensorboard_dir"] == f"persistent/runs/run_{n}"
        with open(os.path.join(projects_dir, project, row["persistent_dir"], "marker.txt")) as f:
            assert f.read() == f"pk={pk}"
    assert not any(
        name.startswith(".migrating_")
        for p in ["a", "b"]
        for name in os.listdir(os.path.join(projects_dir, p, "persistent", "runs"))
    )


def test_folder_migration_defers_when_a_run_process_is_live(app, mocker):
    from services.db_service import get_db
    _reset_folder_migration()
    projects_dir = app.config["PROJECTS_DIR"]
    _mk_run(get_db(), projects_dir, "a")
    mocker.patch.object(rfm, "_live_run_dirs",
                        return_value=[os.path.join(projects_dir, "a", "persistent", "runs", "run_1")])

    assert rfm.migrate_run_folders(projects_dir) == "deferred"
    assert not get_db().migration_applied(rfm.RUN_FOLDER_MIGRATION)


def test_folder_migration_ignores_stale_running_rows(app, mocker):
    from services.db_service import get_db
    _reset_folder_migration()
    db = get_db()
    projects_dir = app.config["PROJECTS_DIR"]
    pk, _ = _mk_run(db, projects_dir, "a")
    db.update_training_run(pk, status="running")  # stale row, no live process
    mocker.patch.object(rfm, "_live_run_dirs", return_value=[])

    assert rfm.migrate_run_folders(projects_dir) == "done"


def test_folder_migration_runs_once(app, mocker):
    _reset_folder_migration()
    mocker.patch.object(rfm, "_live_run_dirs", return_value=[])
    projects_dir = app.config["PROJECTS_DIR"]
    rfm.migrate_run_folders(projects_dir)
    assert rfm.migrate_run_folders(projects_dir) == "already_applied"


def test_folder_migration_tolerates_missing_folder(app, mocker):
    from services.db_service import get_db
    _reset_folder_migration()
    db = get_db()
    projects_dir = app.config["PROJECTS_DIR"]
    pk, n = _mk_run(db, projects_dir, "a", pk_dir=False)
    mocker.patch.object(rfm, "_live_run_dirs", return_value=[])

    assert rfm.migrate_run_folders(projects_dir) == "done"
    assert db.get_run("a", n)["persistent_dir"] == f"persistent/runs/run_{n}"


def test_folder_migration_resumes_after_pass_one(app, mocker):
    # Simulate a crash after pass 1: folder staged with marker, DB still says run_<pk>.
    from services.db_service import get_db
    _reset_folder_migration()
    db = get_db()
    projects_dir = app.config["PROJECTS_DIR"]
    _mk_run(db, projects_dir, "z", pk_dir=False)
    pk, n = _mk_run(db, projects_dir, "a")              # pk 2, #1
    base = os.path.join(projects_dir, "a", "persistent", "runs")
    with open(os.path.join(base, f"run_{pk}", rfm.MARKER), "w") as f:
        f.write(str(pk))
    os.rename(os.path.join(base, f"run_{pk}"), os.path.join(base, f".migrating_{pk}"))
    mocker.patch.object(rfm, "_live_run_dirs", return_value=[])

    assert rfm.migrate_run_folders(projects_dir) == "done"
    assert db.get_run("a", n)["persistent_dir"] == f"persistent/runs/run_{n}"
    assert open(os.path.join(base, f"run_{n}", "marker.txt")).read() == f"pk={pk}"


def test_folder_migration_adopts_folder_moved_before_db_update(app, mocker):
    from services.db_service import get_db
    _reset_folder_migration()
    db = get_db()
    projects_dir = app.config["PROJECTS_DIR"]
    _mk_run(db, projects_dir, "z", pk_dir=False)
    pk, n = _mk_run(db, projects_dir, "a")
    base = os.path.join(projects_dir, "a", "persistent", "runs")
    with open(os.path.join(base, f"run_{pk}", rfm.MARKER), "w") as f:
        f.write(str(pk))
    os.rename(os.path.join(base, f"run_{pk}"), os.path.join(base, f"run_{n}"))  # moved, DB not updated
    mocker.patch.object(rfm, "_live_run_dirs", return_value=[])

    assert rfm.migrate_run_folders(projects_dir) == "done"
    assert db.get_run("a", n)["persistent_dir"] == f"persistent/runs/run_{n}"


def test_folder_migration_never_points_run_at_foreign_folder(app, mocker):
    from services.db_service import get_db
    _reset_folder_migration()
    db = get_db()
    projects_dir = app.config["PROJECTS_DIR"]
    _mk_run(db, projects_dir, "z", pk_dir=False)
    pk, n = _mk_run(db, projects_dir, "a", pk_dir=False)    # own folder missing
    os.makedirs(os.path.join(projects_dir, "a", "persistent", "runs", f"run_{n}"))  # unrelated, no marker
    mocker.patch.object(rfm, "_live_run_dirs", return_value=[])

    assert rfm.migrate_run_folders(projects_dir) == "done"
    assert db.get_run("a", n)["persistent_dir"] is None


def test_folder_migration_reports_conflict_and_retries(app, mocker):
    from services.db_service import get_db
    _reset_folder_migration()
    db = get_db()
    projects_dir = app.config["PROJECTS_DIR"]
    _mk_run(db, projects_dir, "z", pk_dir=False)
    pk, n = _mk_run(db, projects_dir, "a")
    os.makedirs(os.path.join(projects_dir, "a", "persistent", "runs", f".migrating_{pk}"))  # both exist
    mocker.patch.object(rfm, "_live_run_dirs", return_value=[])

    assert rfm.migrate_run_folders(projects_dir) == "conflicts"
    assert not db.migration_applied(rfm.RUN_FOLDER_MIGRATION)
    assert db.get_run("a", n)["persistent_dir"] == f"persistent/runs/run_{pk}"   # untouched
