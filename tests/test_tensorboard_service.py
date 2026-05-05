from unittest.mock import MagicMock, patch

from services.tensorboard_service import parse_run_metrics


def test_parse_run_metrics_uses_project_relative_persistent_tensorboard_dir(tmp_path):
    projects_dir = tmp_path / "projects"
    tb_dir = projects_dir / "myproject" / "persistent" / "runs" / "run_2"
    tb_dir.mkdir(parents=True)

    mock_db = MagicMock()
    mock_db.get_training_run.return_value = {
        "id": 2,
        "project_name": "myproject",
        "started_at": "2026-05-04T12:00:00",
        "persistent_dir": "persistent/runs/run_2",
        "tensorboard_dir": "persistent/runs/run_2",
    }

    with patch("services.tensorboard_service.get_db", return_value=mock_db):
        result = parse_run_metrics(str(projects_dir), "myproject", 2)

    assert result["success"] is False
    assert result["reason"] == "no_event_files"
    assert result["path"] == str(tb_dir)


def test_parse_run_metrics_falls_back_to_legacy_workspace_tensorboard_dir(tmp_path):
    projects_dir = tmp_path / "projects"
    tb_dir = projects_dir / "myproject" / "workspace" / "runs" / "run_2"
    tb_dir.mkdir(parents=True)

    mock_db = MagicMock()
    mock_db.get_training_run.return_value = {
        "id": 2,
        "project_name": "myproject",
        "started_at": "2026-05-04T12:00:00",
        "tensorboard_dir": "runs/run_2",
    }

    with patch("services.tensorboard_service.get_db", return_value=mock_db):
        result = parse_run_metrics(str(projects_dir), "myproject", 2)

    assert result["success"] is False
    assert result["reason"] == "no_event_files"
    assert result["path"] == str(tb_dir)
