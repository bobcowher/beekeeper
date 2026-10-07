import json
import os

import pytest

from services.project_service import validate_output_paths, rename_project


def _write_project(projects_dir, name, **overrides):
    project_dir = os.path.join(projects_dir, name)
    os.makedirs(project_dir, exist_ok=True)
    data = {
        "name": name,
        "git_url": "https://github.com/user/repo.git",
        "env_type": "venv",
        "setup_status": "ready",
    }
    data.update(overrides)
    with open(os.path.join(project_dir, "project.json"), "w") as f:
        json.dump(data, f)
    return data


def _inline_thread(**kwargs):
    """Thread mock that runs target synchronously when start() is called."""
    target = kwargs["target"]
    args = kwargs.get("args", ())

    class _T:
        def start(self):
            target(*args)

    return _T()


def test_rename_project_moves_directory_and_updates_run_history(tmp_path, mocker):
    projects_dir = str(tmp_path)
    _write_project(projects_dir, "oldname")

    db = mocker.MagicMock()
    mocker.patch("services.db_service.get_db", return_value=db)

    result = rename_project(projects_dir, "oldname", "newname")

    assert result["name"] == "newname"
    assert not os.path.exists(os.path.join(projects_dir, "oldname"))
    assert os.path.isdir(os.path.join(projects_dir, "newname"))
    db.rename_project_runs.assert_called_once_with("oldname", "newname")


def test_rename_conda_project_removes_old_env_and_rebuilds_in_background(tmp_path, mocker):
    from services import project_service

    projects_dir = str(tmp_path)
    _write_project(projects_dir, "oldname", env_type="conda")

    mocker.patch("services.db_service.get_db", return_value=mocker.MagicMock())
    mocker.patch.object(project_service, "_find_conda_bin", return_value="/usr/bin/conda")
    run_spy = mocker.patch("services.project_service.subprocess.run")
    setup_spy = mocker.patch.object(project_service, "_setup_project")
    mocker.patch("services.project_service.threading.Thread", side_effect=_inline_thread)

    result = rename_project(projects_dir, "oldname", "newname")

    assert result["setup_status"] == "pending"

    run_spy.assert_called_once()
    assert run_spy.call_args[0][0] == ["/usr/bin/conda", "env", "remove", "-y", "-n", "beekeeper-oldname"]

    setup_spy.assert_called_once()
    called_projects_dir, called_project, called_is_retry = setup_spy.call_args[0]
    assert called_projects_dir == projects_dir
    assert called_project.name == "newname"
    assert called_is_retry is True

    on_disk = json.load(open(os.path.join(projects_dir, "newname", "project.json")))
    assert on_disk["setup_status"] == "pending"


def test_rename_venv_project_skips_conda_and_background_rebuild(tmp_path, mocker):
    from services import project_service

    projects_dir = str(tmp_path)
    _write_project(projects_dir, "oldname", env_type="venv", setup_status="ready")

    mocker.patch("services.db_service.get_db", return_value=mocker.MagicMock())
    conda_spy = mocker.patch.object(project_service, "_find_conda_bin")
    thread_spy = mocker.patch("services.project_service.threading.Thread")

    result = rename_project(projects_dir, "oldname", "newname")

    assert result["setup_status"] == "ready"
    conda_spy.assert_not_called()
    thread_spy.assert_not_called()


def test_validate_output_paths_normalizes_form_text():
    assert validate_output_paths("saved_models\nexports/checkpoints", "runs") == [
        "saved_models",
        "exports/checkpoints",
    ]


@pytest.mark.parametrize(
    "paths",
    [
        ["/tmp/models"],
        ["../models"],
        ["models/../exports"],
        ["models//exports"],
        ["."],
        ["persistent/models"],
        ["run_logs/archive"],
        [".git/hooks"],
        ["models", "models/checkpoints"],
        ["runs/scalars"],
    ],
)
def test_validate_output_paths_rejects_unsafe_paths(paths):
    with pytest.raises(ValueError):
        validate_output_paths(paths, "runs")


def test_delete_project_purges_run_history(tmp_path, mocker):
    from services import project_service
    projects_dir = str(tmp_path)
    _write_project(projects_dir, "gone")
    db = mocker.MagicMock()
    mocker.patch("services.db_service.get_db", return_value=db)
    mocker.patch("services.process_manager.get_runs_for_project", return_value=[])

    project_service.delete_project(projects_dir, "gone")

    db.delete_project_runs.assert_called_once_with("gone")


def test_delete_project_refuses_while_a_run_is_active(tmp_path, mocker):
    from services import project_service
    projects_dir = str(tmp_path)
    _write_project(projects_dir, "busy")
    mocker.patch("services.process_manager.get_runs_for_project",
                 return_value=[{"run_id": 1, "status": "starting"}])

    with pytest.raises(project_service.ProjectBusyError):
        project_service.delete_project(projects_dir, "busy")
    assert os.path.isdir(os.path.join(projects_dir, "busy"))


def test_rename_purges_orphan_history_at_destination(tmp_path, mocker):
    projects_dir = str(tmp_path)
    _write_project(projects_dir, "oldname")
    db = mocker.MagicMock()
    mocker.patch("services.db_service.get_db", return_value=db)

    rename_project(projects_dir, "oldname", "newname")

    assert db.method_calls[:2] == [
        mocker.call.delete_project_runs("newname"),
        mocker.call.rename_project_runs("oldname", "newname"),
    ]
