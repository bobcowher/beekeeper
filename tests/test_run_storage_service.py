from services.run_storage_service import clear_persistent_runs, delete_run_storage


def test_delete_run_storage_removes_persistent_run_dir_and_archived_log(tmp_path):
    projects_dir = tmp_path / "projects"
    project_dir = projects_dir / "myproject"
    persistent_dir = project_dir / "persistent" / "runs" / "run_4"
    persistent_dir.mkdir(parents=True)
    (persistent_dir / "events.out.tfevents.1").write_text("event")
    log_dir = project_dir / "run_logs"
    log_dir.mkdir()
    (log_dir / "run.log").write_text("log")

    delete_run_storage(
        str(projects_dir),
        "myproject",
        {
            "persistent_dir": "persistent/runs/run_4",
            "log_file_path": "run_logs/run.log",
        },
    )

    assert not persistent_dir.exists()
    assert not (log_dir / "run.log").exists()


def test_clear_persistent_runs_removes_orphans_and_recreates_root(tmp_path):
    projects_dir = tmp_path / "projects"
    orphan = projects_dir / "myproject" / "persistent" / "runs" / "run_orphan"
    orphan.mkdir(parents=True)
    (orphan / "events.out.tfevents.1").write_text("event")

    clear_persistent_runs(str(projects_dir), "myproject")

    runs_root = projects_dir / "myproject" / "persistent" / "runs"
    assert runs_root.is_dir()
    assert list(runs_root.iterdir()) == []
