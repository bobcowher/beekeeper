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


def test_delete_run_storage_refuses_folder_owned_by_another_run(tmp_path):
    from services.run_storage_service import MARKER
    projects_dir = tmp_path / "projects"
    folder = projects_dir / "myproject" / "persistent" / "runs" / "run_4"
    folder.mkdir(parents=True)
    (folder / MARKER).write_text("9")
    (folder / "model.pt").write_text("weights")

    delete_run_storage(str(projects_dir), "myproject",
                       {"id": 4, "persistent_dir": "persistent/runs/run_4"})
    assert (folder / "model.pt").exists()

    delete_run_storage(str(projects_dir), "myproject",
                       {"id": 9, "persistent_dir": "persistent/runs/run_4"})
    assert not folder.exists()
