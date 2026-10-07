import os
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
    with open(os.path.join(base, f"run_{n}", "marker.txt")) as f:
        assert f.read() == f"pk={pk}"


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


def _read(path):
    with open(path) as f:
        return f.read()


def _swapped_runs(db, projects_dir):
    """Project a: pk1 -> #2 and pk2 -> #1 (run_number > id for one row)."""
    pk1, _ = _mk_run(db, projects_dir, "a")
    pk2, _ = _mk_run(db, projects_dir, "a")
    with db._get_connection() as c:
        c.execute("UPDATE training_runs SET run_number = 99 WHERE id = ?", (pk1,))
        c.execute("UPDATE training_runs SET run_number = 1 WHERE id = ?", (pk2,))
        c.execute("UPDATE training_runs SET run_number = 2 WHERE id = ?", (pk1,))
        c.commit()
    return pk1, pk2


def _assert_swapped(db, projects_dir, pk1, pk2):
    base = os.path.join(projects_dir, "a", "persistent", "runs")
    assert db.get_run("a", 2)["persistent_dir"] == "persistent/runs/run_2"
    assert db.get_run("a", 1)["persistent_dir"] == "persistent/runs/run_1"
    assert _read(os.path.join(base, "run_2", "marker.txt")) == f"pk={pk1}"
    assert _read(os.path.join(base, "run_1", "marker.txt")) == f"pk={pk2}"
    assert not [n for n in os.listdir(base) if n.startswith(".migrating_")]


def test_folder_migration_handles_run_number_greater_than_pk(app, mocker):
    from services.db_service import get_db
    _reset_folder_migration()
    db = get_db()
    projects_dir = app.config["PROJECTS_DIR"]
    pk1, pk2 = _swapped_runs(db, projects_dir)
    mocker.patch.object(rfm, "_live_run_dirs", return_value=[])

    assert rfm.migrate_run_folders(projects_dir) == "done"
    _assert_swapped(db, projects_dir, pk1, pk2)


def test_folder_migration_rerun_after_kill_mid_swap(app, mocker):
    from services.db_service import get_db
    _reset_folder_migration()
    db = get_db()
    projects_dir = app.config["PROJECTS_DIR"]
    pk1, pk2 = _swapped_runs(db, projects_dir)
    mocker.patch.object(rfm, "_live_run_dirs", return_value=[])
    base = os.path.join(projects_dir, "a", "persistent", "runs")
    # Kill state: pass 1 staged both; pass 2 placed pk2 at run_1 only (DB updated for it).
    for pk in (pk1, pk2):
        with open(os.path.join(base, f"run_{pk}", rfm.MARKER), "w") as f:
            f.write(str(pk))
        os.rename(os.path.join(base, f"run_{pk}"), os.path.join(base, f".migrating_{pk}"))
    os.rename(os.path.join(base, f".migrating_{pk2}"), os.path.join(base, "run_1"))
    db.update_training_run(pk2, persistent_dir="persistent/runs/run_1",
                           tensorboard_dir="persistent/runs/run_1")

    assert rfm.migrate_run_folders(projects_dir) == "done"
    _assert_swapped(db, projects_dir, pk1, pk2)


def test_folder_migration_rerun_does_not_steal_placed_folder(app, mocker):
    # Kill state: pk1 placed at run_2 (DB still stale), pk2 still staged. A buggy pass 1
    # would treat run_2 (pk1's folder, carrying pk1's marker) as pk2's source.
    from services.db_service import get_db
    _reset_folder_migration()
    db = get_db()
    projects_dir = app.config["PROJECTS_DIR"]
    pk1, pk2 = _swapped_runs(db, projects_dir)
    mocker.patch.object(rfm, "_live_run_dirs", return_value=[])
    base = os.path.join(projects_dir, "a", "persistent", "runs")
    for pk in (pk1, pk2):
        with open(os.path.join(base, f"run_{pk}", rfm.MARKER), "w") as f:
            f.write(str(pk))
        os.rename(os.path.join(base, f"run_{pk}"), os.path.join(base, f".migrating_{pk}"))
    os.rename(os.path.join(base, f".migrating_{pk1}"), os.path.join(base, "run_2"))  # pk1 placed, DB stale

    assert rfm.migrate_run_folders(projects_dir) == "done"
    _assert_swapped(db, projects_dir, pk1, pk2)


def test_create_app_survives_migration_failure(mocker, tmp_path):
    import app as app_module
    mocker.patch("services.run_folder_migration.migrate_run_folders", side_effect=OSError("boom"))
    application = app_module.create_app({
        "TESTING": True,
        "DB_PATH": str(tmp_path / "t.db"),
        "PROJECTS_DIR": str(tmp_path / "projects"),
    })
    assert application is not None


def _link_setup(db, projects_dir, output_paths=()):
    import json
    _mk_run(db, projects_dir, "z", pk_dir=False)
    pk, n = _mk_run(db, projects_dir, "a")
    pdir = os.path.join(projects_dir, "a")
    with open(os.path.join(pdir, "project.json"), "w") as f:
        json.dump({"tensorboard_log_dir": "runs", "output_paths": list(output_paths)}, f)
    old = os.path.join(pdir, "persistent", "runs", f"run_{pk}")
    for ws in ("workspace", f"workspace-{pk}"):
        os.makedirs(os.path.join(pdir, ws))
        os.symlink(old, os.path.join(pdir, ws, "runs"))
    return pk, n, pdir, old


def test_folder_migration_repoints_workspace_symlinks(app, mocker):
    from services.db_service import get_db
    _reset_folder_migration()
    db = get_db()
    projects_dir = app.config["PROJECTS_DIR"]
    pk, n, pdir, old = _link_setup(db, projects_dir, output_paths=["out/models"])
    os.makedirs(os.path.join(old, "out", "models"))
    os.symlink(os.path.join(old, "out", "models"), os.path.join(pdir, "workspace", "models_link"))
    os.makedirs(os.path.join(pdir, "workspace", "out"))
    os.symlink(os.path.join(old, "out", "models"), os.path.join(pdir, "workspace", "out", "models"))
    # An unrelated link must stay untouched.
    elsewhere = os.path.join(pdir, "elsewhere")
    os.makedirs(elsewhere)
    os.symlink(elsewhere, os.path.join(pdir, "workspace-other-link"))
    mocker.patch.object(rfm, "_live_run_dirs", return_value=[])

    assert rfm.migrate_run_folders(projects_dir) == "done"
    new = os.path.join(pdir, "persistent", "runs", f"run_{n}")
    assert os.readlink(os.path.join(pdir, "workspace", "runs")) == new
    assert os.readlink(os.path.join(pdir, f"workspace-{pk}", "runs")) == new
    assert os.readlink(os.path.join(pdir, "workspace", "out", "models")) == os.path.join(new, "out", "models")
    assert os.readlink(os.path.join(pdir, "workspace-other-link")) == elsewhere
    assert os.path.isdir(os.path.join(pdir, "workspace", "runs"))


def test_folder_migration_skips_links_without_project_json(app, mocker):
    from services.db_service import get_db
    _reset_folder_migration()
    db = get_db()
    projects_dir = app.config["PROJECTS_DIR"]
    pk, n, pdir, old = _link_setup(db, projects_dir)
    os.remove(os.path.join(pdir, "project.json"))
    mocker.patch.object(rfm, "_live_run_dirs", return_value=[])

    assert rfm.migrate_run_folders(projects_dir) == "done"
    assert os.readlink(os.path.join(pdir, "workspace", "runs")) == old


def _write_marker(folder, pk):
    with open(os.path.join(folder, rfm.MARKER), "w") as f:
        f.write(str(pk))


def test_folder_migration_occupied_dest_points_row_at_staged_folder(app, mocker):
    from services.db_service import get_db
    from services.run_storage_service import delete_run_storage
    _reset_folder_migration()
    db = get_db()
    projects_dir = app.config["PROJECTS_DIR"]
    _mk_run(db, projects_dir, "z", pk_dir=False)            # pk 1
    old_pk, old_n = _mk_run(db, projects_dir, "a")          # pk 2, a #1
    base = os.path.join(projects_dir, "a", "persistent", "runs")
    foreign = os.path.join(base, f"run_{old_n}")
    os.makedirs(foreign)
    open(os.path.join(foreign, "foreign.txt"), "w").close()  # dest occupied, no marker
    mocker.patch.object(rfm, "_live_run_dirs", return_value=[])

    assert rfm.migrate_run_folders(projects_dir) == "conflicts"
    staged_rel = f"persistent/runs/.migrating_{old_pk}"
    row = db.get_training_run(old_pk)
    assert row["persistent_dir"] == staged_rel
    assert row["tensorboard_dir"] == staged_rel

    # A new run gets number == old pk and creates run_<old pk> with its own marker.
    new_pk, new_n = db.create_training_run("a", _meta())
    assert new_n == old_pk
    new_folder = os.path.join(base, f"run_{new_n}")
    os.makedirs(new_folder)
    _write_marker(new_folder, new_pk)
    open(os.path.join(new_folder, "new.txt"), "w").close()

    # Even a stale pointer at run_<old pk> must not delete the new run's folder.
    delete_run_storage(projects_dir, "a", {**row, "persistent_dir": f"persistent/runs/run_{old_pk}"})
    assert os.path.isfile(os.path.join(new_folder, "new.txt"))

    # Resolve the conflict; the next startup places the staged folder and finishes.
    import shutil
    shutil.rmtree(foreign)
    assert rfm.migrate_run_folders(projects_dir) == "done"
    assert db.migration_applied(rfm.RUN_FOLDER_MIGRATION)
    row = db.get_training_run(old_pk)
    assert row["persistent_dir"] == f"persistent/runs/run_{old_n}"
    assert _read(os.path.join(base, f"run_{old_n}", "marker.txt")) == f"pk={old_pk}"

    # Pruning the old row removes only its own folder.
    delete_run_storage(projects_dir, "a", row)
    assert not os.path.exists(os.path.join(base, f"run_{old_n}"))
    assert os.path.isfile(os.path.join(new_folder, "new.txt"))


def test_folder_migration_oserror_in_pass_one_does_not_block_other_rows(app, mocker):
    from services.db_service import get_db
    _reset_folder_migration()
    db = get_db()
    projects_dir = app.config["PROJECTS_DIR"]
    pk1, pk2 = _swapped_runs(db, projects_dir)              # a: pk1 -> #2, pk2 -> #1
    b_pk, b_n = _mk_run(db, projects_dir, "b")              # b: pk3 -> #1
    stuck = os.path.join(projects_dir, "a", "persistent", "runs", f"run_{pk1}")
    real_rename = os.rename

    def flaky_rename(src, dst, *args, **kwargs):
        if os.path.normpath(src) == os.path.normpath(stuck):
            raise OSError(13, "Permission denied")
        return real_rename(src, dst, *args, **kwargs)

    mocker.patch("services.run_folder_migration.os.rename", side_effect=flaky_rename)
    mocker.patch.object(rfm, "_live_run_dirs", return_value=[])

    assert rfm.migrate_run_folders(projects_dir) == "conflicts"
    assert not db.migration_applied(rfm.RUN_FOLDER_MIGRATION)
    assert db.get_run("b", b_n)["persistent_dir"] == f"persistent/runs/run_{b_n}"
    for pk in (pk1, pk2, b_pk):
        row = db.get_training_run(pk)
        folder = os.path.join(projects_dir, row["project_name"], row["persistent_dir"])
        assert _read(os.path.join(folder, "marker.txt")) == f"pk={pk}"


def test_folder_migration_archives_legacy_unarchived_live_log(app, mocker):
    from services.db_service import get_db
    from services.process_manager import get_run_log_path
    _reset_folder_migration()
    db = get_db()
    projects_dir = app.config["PROJECTS_DIR"]
    _mk_run(db, projects_dir, "z", pk_dir=False)            # pk 1
    _mk_run(db, projects_dir, "z", pk_dir=False)            # pk 2
    old_pk, old_n = _mk_run(db, projects_dir, "a", pk_dir=False)   # pk 3, a #1
    new_pk, new_n = _mk_run(db, projects_dir, "a", pk_dir=False)   # pk 4, a #2
    pdir = os.path.join(projects_dir, "a")
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, f"train-{old_pk}.log"), "w") as f:
        f.write("old run log")
    with open(os.path.join(pdir, f"train-{new_n}.log"), "w") as f:
        f.write("new run log")
    mocker.patch.object(rfm, "_live_run_dirs", return_value=[])

    assert rfm.migrate_run_folders(projects_dir) == "done"
    rel = db.get_training_run(old_pk)["log_file_path"]
    assert rel.startswith("run_logs/run-") and rel.endswith(f"-{old_n:04d}.log")
    assert not os.path.exists(os.path.join(pdir, f"train-{old_pk}.log"))
    assert _read(get_run_log_path(projects_dir, "a", old_n)) == "old run log"
    assert _read(os.path.join(pdir, f"train-{new_n}.log")) == "new run log"
    assert _read(get_run_log_path(projects_dir, "a", new_n)) == "new run log"
    assert db.get_training_run(new_pk)["log_file_path"] is None


def test_folder_migration_leaves_ambiguous_legacy_log(app, mocker):
    # train-<pk>.log where another run in the project has number == pk: that run (new code)
    # already truncated and owns the file, so it must not be handed to the old row.
    from services.db_service import get_db
    _reset_folder_migration()
    db = get_db()
    projects_dir = app.config["PROJECTS_DIR"]
    _mk_run(db, projects_dir, "z", pk_dir=False)            # pk 1
    old_pk, _ = _mk_run(db, projects_dir, "a", pk_dir=False)       # pk 2, a #1
    owner_pk, owner_n = _mk_run(db, projects_dir, "a", pk_dir=False)  # pk 3, a #2
    assert owner_n == old_pk
    pdir = os.path.join(projects_dir, "a")
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, f"train-{old_pk}.log"), "w") as f:
        f.write("owned by a #2")
    mocker.patch.object(rfm, "_live_run_dirs", return_value=[])

    assert rfm.migrate_run_folders(projects_dir) == "done"
    assert db.get_training_run(old_pk)["log_file_path"] is None
    assert _read(os.path.join(pdir, f"train-{owner_n}.log")) == "owned by a #2"
