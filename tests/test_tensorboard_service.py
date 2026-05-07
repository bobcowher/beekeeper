from unittest.mock import MagicMock, patch

import pytest

from services.tensorboard_service import (
    analyze_metric,
    cleanup_old_tb_logs,
    parse_run_metrics,
    smart_sample,
)


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


def test_analyze_metric_reports_smoothed_peak_and_recent_trend():
    data = [(step, float(step), float(step * 10)) for step in range(1, 61)]

    result = analyze_metric("episode_reward", data)

    assert result["trend"] == "improving"
    assert result["recent_trend"] == "improving"
    assert result["ema_alpha"] == pytest.approx(0.9)
    assert result["peak_value"] == pytest.approx(result["smoothed_final_value"])
    assert result["peak_step"] == 60
    assert result["peak_reversal_pct"] == pytest.approx(0.0)
    assert result["late_slope_pct"] > 0
    assert result["smoothed_points"][-1]["step"] == 60
    assert "overall increased" in result["summary"]


def test_analyze_metric_uses_lower_better_peak_reversal_for_loss():
    values = [10.0 - step for step in range(10)] + [2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
    data = [(step, value, float(step)) for step, value in enumerate(values, start=1)]

    result = analyze_metric("validation_loss", data)

    assert result["best_value"] == pytest.approx(1.0)
    assert result["best_step"] == 10
    assert result["peak_value"] < result["initial_value"]
    assert result["peak_reversal_pct"] > 0


def test_smart_sample_includes_inflection_points_for_long_series():
    data = [
        (step, float((step % 6) - 3), float(step))
        for step in range(150)
    ]

    sample = smart_sample(data, target=25)

    assert len(sample) <= 25
    assert sample[0]["step"] == 0
    assert sample[-1]["step"] == 149
    assert any(point["step"] not in (0, 149) for point in sample)


def test_cleanup_old_tb_logs_keeps_recent_and_protected_runs(tmp_path):
    tb_dir = tmp_path / "runs"
    tb_dir.mkdir()
    for dirname in [
        "20260501-120000",
        "20260502-120000",
        "2026-05-03_12-00-00_train",
        "not-a-run",
    ]:
        (tb_dir / dirname).mkdir()
    (tb_dir / "README.txt").write_text("not a run directory")

    result = cleanup_old_tb_logs(
        str(tb_dir),
        keep_count=1,
        protected_dirs={"20260501-120000"},
    )

    assert result["deleted"] == ["20260502-120000"]
    assert set(result["kept"]) == {"2026-05-03_12-00-00_train", "20260501-120000"}
    assert (tb_dir / "2026-05-03_12-00-00_train").is_dir()
    assert (tb_dir / "20260501-120000").is_dir()
    assert not (tb_dir / "20260502-120000").exists()
    assert (tb_dir / "not-a-run").is_dir()


def test_cleanup_old_tb_logs_handles_no_limit_and_missing_dir(tmp_path):
    tb_dir = tmp_path / "runs"
    tb_dir.mkdir()

    assert cleanup_old_tb_logs(str(tb_dir), keep_count=0)["message"] == "No limit set, nothing deleted"
    assert cleanup_old_tb_logs(str(tmp_path / "missing"), keep_count=1)["message"] == "TensorBoard log directory not found"
