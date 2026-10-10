"""
Tests for start_training() in process_manager.
Focuses on the pre-launch sequence: git sync → pip install → subprocess start.

start_training() is now async: it validates, reserves the slot, and launches
_execute_training() in a background thread. Tests use _inline_thread() to run
the background work synchronously so assertions can inspect the final state.
"""
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from services import process_manager as pm
from services.process_manager import start_training
from tests.conftest import make_project_dir


def _make_project(tmp_path, **overrides):
    """Create a minimal ready project on disk."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(exist_ok=True)
    proj_dir = projects_dir / "myproject"
    proj_dir.mkdir()
    src_dir = proj_dir / "workspace"
    src_dir.mkdir()
    (src_dir / "train.py").write_text("print('training')")
    (src_dir / "requirements.txt").write_text("numpy\n")

    data = {
        "name": "myproject",
        "git_url": "https://github.com/user/repo.git",
        "branch": "main",
        "python_version": "3.11",
        "train_file": "train.py",
        "tensorboard_log_dir": "runs",
        "requirements_file": "requirements.txt",
        "env_type": "venv",
        "setup_status": "ready",
        "setup_error": "",
        "train_status": "idle",
        "train_pid": 0,
        "env_vars": {},
        "parallel_runs_enabled": False,
        "max_parallel_runs": 2,
        "output_paths": [],
    }
    data.update(overrides)
    (proj_dir / "project.json").write_text(json.dumps(data))
    return str(projects_dir)


def _ok_run(returncode=0, stderr="", stdout=""):
    m = MagicMock()
    m.returncode = returncode
    m.stderr = stderr
    m.stdout = stdout
    return m


def _inline_thread(**kwargs):
    """Thread mock that runs target synchronously when start() is called."""
    target = kwargs.get("target") or (lambda: None)
    args = kwargs.get("args", ())

    class _T:
        def start(self):
            target(*args)

    return _T()


# --- pip install called ---

def test_pip_install_called_with_requirements_file(tmp_path):
    """pip install must be called with the project's requirements file."""
    from services import process_manager
    process_manager._running.clear()
    projects_dir = _make_project(tmp_path)
    mock_db = MagicMock(create_training_run=MagicMock(return_value=(99, 99)),
                        delete_training_run=MagicMock(),
                        get_training_runs=MagicMock(return_value=[]))

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager.subprocess.run", return_value=_ok_run()) as mock_run, \
         patch("services.process_manager.subprocess.Popen") as mock_popen, \
         patch("services.process_manager._update_project_json"), \
         patch("services.process_manager._monitor_process"), \
         patch("services.process_manager.threading.Thread", side_effect=_inline_thread), \
         patch("services.process_manager.get_db", return_value=mock_db):

        mock_proc = MagicMock()
        mock_proc.pid = 9999
        mock_popen.return_value = mock_proc

        start_training(projects_dir, "myproject")
    process_manager._running.clear()

    calls = [c.args[0] for c in mock_run.call_args_list]
    pip_calls = [c for c in calls if "pip" in c]
    assert len(pip_calls) == 1
    assert "-m" in pip_calls[0]
    assert "pip" in pip_calls[0]
    assert "install" in pip_calls[0]
    assert "--upgrade" in pip_calls[0]
    assert "--upgrade-strategy" in pip_calls[0]
    assert "only-if-needed" in pip_calls[0]
    assert "-r" in pip_calls[0]
    assert any("requirements.txt" in arg for arg in pip_calls[0])


def test_pip_install_skipped_when_no_requirements_file(tmp_path):
    """If requirements.txt doesn't exist, pip install is silently skipped."""
    from services import process_manager
    process_manager._running.clear()
    projects_dir = _make_project(tmp_path)
    os.remove(os.path.join(projects_dir, "myproject", "workspace", "requirements.txt"))
    mock_db = MagicMock(create_training_run=MagicMock(return_value=(99, 99)),
                        delete_training_run=MagicMock(),
                        get_training_runs=MagicMock(return_value=[]))

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager.subprocess.run", return_value=_ok_run()) as mock_run, \
         patch("services.process_manager.subprocess.Popen") as mock_popen, \
         patch("services.process_manager._update_project_json"), \
         patch("services.process_manager._monitor_process"), \
         patch("services.process_manager.threading.Thread", side_effect=_inline_thread), \
         patch("services.process_manager.get_db", return_value=mock_db):

        mock_proc = MagicMock()
        mock_proc.pid = 9999
        mock_popen.return_value = mock_proc

        start_training(projects_dir, "myproject")
    process_manager._running.clear()

    calls = [c.args[0] for c in mock_run.call_args_list]
    pip_calls = [c for c in calls if "pip" in c]
    assert len(pip_calls) == 0


# --- pip install failure ---

def test_pip_install_failure_blocks_training(tmp_path):
    """A non-zero pip exit code must not launch the process."""
    from services import process_manager
    process_manager._running.clear()
    projects_dir = _make_project(tmp_path)
    mock_db = MagicMock(create_training_run=MagicMock(return_value=(99, 99)),
                        delete_training_run=MagicMock(),
                        get_training_runs=MagicMock(return_value=[]))

    def fake_run(cmd, **kwargs):
        if "pip" in cmd:
            return _ok_run(returncode=1, stderr="ERROR: Could not find a version that satisfies the requirement fakepkg")
        return _ok_run()

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager.subprocess.run", side_effect=fake_run), \
         patch("services.process_manager.subprocess.Popen") as mock_popen, \
         patch("services.process_manager._update_project_json"), \
         patch("services.process_manager.threading.Thread", side_effect=_inline_thread), \
         patch("services.process_manager.get_db", return_value=mock_db):

        start_training(projects_dir, "myproject")

    mock_popen.assert_not_called()
    process_manager._running.clear()


def test_pip_install_timeout_blocks_training(tmp_path):
    """A pip install timeout must not launch the process."""
    import subprocess as _subprocess
    from services import process_manager
    process_manager._running.clear()
    projects_dir = _make_project(tmp_path)
    mock_db = MagicMock(create_training_run=MagicMock(return_value=(99, 99)),
                        delete_training_run=MagicMock(),
                        get_training_runs=MagicMock(return_value=[]))

    def fake_run(cmd, **kwargs):
        if "pip" in cmd:
            raise _subprocess.TimeoutExpired(cmd, 300)
        return _ok_run()

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager.subprocess.run", side_effect=fake_run), \
         patch("services.process_manager.subprocess.Popen") as mock_popen, \
         patch("services.process_manager._update_project_json"), \
         patch("services.process_manager.threading.Thread", side_effect=_inline_thread), \
         patch("services.process_manager.get_db", return_value=mock_db):

        start_training(projects_dir, "myproject")

    mock_popen.assert_not_called()
    process_manager._running.clear()


# --- pip runs after git sync ---

def test_pip_install_runs_after_git_pull(tmp_path):
    """pip install must happen after git sync, not before."""
    from services import process_manager
    process_manager._running.clear()
    projects_dir = _make_project(tmp_path)
    call_order = []
    mock_db = MagicMock(create_training_run=MagicMock(return_value=(99, 99)),
                        delete_training_run=MagicMock(),
                        get_training_runs=MagicMock(return_value=[]))

    def fake_run(cmd, **kwargs):
        if "fetch" in cmd or "reset" in cmd:
            call_order.append("git_sync")
        elif "pip" in cmd:
            call_order.append("pip_install")
        return _ok_run()

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager.subprocess.run", side_effect=fake_run), \
         patch("services.process_manager.subprocess.Popen") as mock_popen, \
         patch("services.process_manager._update_project_json"), \
         patch("services.process_manager._monitor_process"), \
         patch("services.process_manager.threading.Thread", side_effect=_inline_thread), \
         patch("services.process_manager.get_db", return_value=mock_db):

        mock_proc = MagicMock()
        mock_proc.pid = 9999
        mock_popen.return_value = mock_proc

        start_training(projects_dir, "myproject")
    process_manager._running.clear()

    assert call_order[0] == "git_sync"
    assert "pip_install" in call_order
    assert call_order.index("git_sync") < call_order.index("pip_install")


# --- get_runs_for_project ---

def test_get_runs_for_project_empty(tmp_path):
    """Returns empty list when no runs active."""
    from services import process_manager
    # Ensure clean state
    process_manager._running.clear()
    from services.process_manager import get_runs_for_project
    assert get_runs_for_project("nonexistent") == []

def test_get_runs_for_project_returns_active(tmp_path):
    """Returns one entry per active run for the project."""
    from services import process_manager
    process_manager._running.clear()
    mock_proc = MagicMock()
    mock_proc.pid = 1234
    process_manager._running[42] = {
        "process": mock_proc,
        "starting": False,
        "project_name": "myproject",
        "run_pk": 42,
        "run_id": 7,
        "branch": "main",
        "workspace_dir": "/fake/workspace",
        "log_path": "/fake/train.log",
        "tb_port": None,
        "started_at": 0.0,
    }
    from services.process_manager import get_runs_for_project
    with patch("services.resource_tracker.get_process_resources", return_value=None):
        runs = get_runs_for_project("myproject")
    assert len(runs) == 1
    assert runs[0]["run_id"] == 7
    assert runs[0]["run_ref"] == "myproject-7"
    assert "run_pk" not in runs[0]
    assert runs[0]["branch"] == "main"
    assert runs[0]["status"] == "running"
    process_manager._running.clear()


# --- parallel capacity ---

def test_start_training_rejects_second_run_when_parallel_disabled(tmp_path):
    """Second start_training call is rejected when parallel_runs_enabled=False."""
    from services import process_manager
    process_manager._running.clear()
    projects_dir = _make_project(tmp_path)
    mock_db = MagicMock(create_training_run=MagicMock(return_value=(1, 1)),
                        delete_training_run=MagicMock(),
                        get_training_runs=MagicMock(return_value=[]))

    # Simulate run 1 already active
    process_manager._running[1] = {
        "process": MagicMock(), "starting": False,
        "project_name": "myproject", "run_pk": 1,
        "branch": "main",
        "workspace_dir": os.path.join(projects_dir, "myproject", "workspace"),
    }

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager.get_db", return_value=mock_db):
        result = process_manager.start_training(projects_dir, "myproject")

    assert "error" in result
    process_manager._running.clear()


def test_start_training_allows_second_run_when_parallel_enabled(tmp_path):
    """Second start_training is allowed when parallel_runs_enabled=True."""
    from services import process_manager
    process_manager._running.clear()
    projects_dir = _make_project(tmp_path, parallel_runs_enabled=True, max_parallel_runs=2)
    mock_db = MagicMock(create_training_run=MagicMock(return_value=(2, 2)),
                        delete_training_run=MagicMock(),
                        get_training_runs=MagicMock(return_value=[]))

    # Simulate run 1 already active on primary workspace
    process_manager._running[1] = {
        "process": MagicMock(), "starting": False,
        "project_name": "myproject", "run_pk": 1,
        "branch": "main",
        "workspace_dir": os.path.join(projects_dir, "myproject", "workspace"),
    }

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager._update_project_json"), \
         patch("services.process_manager.threading.Thread", side_effect=_inline_thread), \
         patch("services.process_manager.subprocess.run", return_value=_ok_run()), \
         patch("services.process_manager.subprocess.Popen") as mock_popen, \
         patch("services.process_manager._monitor_process"), \
         patch("services.process_manager.get_db", return_value=mock_db):
        mock_popen.return_value = MagicMock(pid=9999)
        result = process_manager.start_training(projects_dir, "myproject", branch="feature/x")

    assert "error" not in result
    assert result.get("run_id") == 2
    process_manager._running.clear()


def test_parallel_tensorboard_link_exists_before_training_process(tmp_path):
    """Parallel runs must redirect TB logs before the training process can create runs/."""
    from services import process_manager
    process_manager._running.clear()
    projects_dir = _make_project(tmp_path, parallel_runs_enabled=True, max_parallel_runs=2)
    parallel_ws = os.path.join(projects_dir, "myproject", "workspace-2")
    os.makedirs(parallel_ws)
    with open(os.path.join(parallel_ws, "train.py"), "w") as f:
        f.write("print('training')")

    mock_db = MagicMock(create_training_run=MagicMock(return_value=(2, 2)),
                        delete_training_run=MagicMock(),
                        get_training_runs=MagicMock(return_value=[]))

    process_manager._running[1] = {
        "process": MagicMock(), "starting": False,
        "project_name": "myproject", "run_pk": 1,
        "branch": "main",
        "workspace_dir": os.path.join(projects_dir, "myproject", "workspace"),
    }

    expected_target = os.path.join(projects_dir, "myproject", "persistent", "runs", "run_2")
    link_path = os.path.join(parallel_ws, "runs")

    def fake_popen(cmd, **kwargs):
        assert os.path.islink(link_path)
        assert os.readlink(link_path) == expected_target
        assert kwargs["env"]["BEEKEEPER_RUN_DIR"] == expected_target
        assert kwargs["env"]["BEEKEEPER_TENSORBOARD_DIR"] == expected_target
        assert kwargs["env"]["TENSORBOARD_LOG_DIR"] == expected_target
        proc = MagicMock()
        proc.pid = 9999
        return proc

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager._resolve_tensorboard_binary", return_value=None), \
         patch("services.process_manager._update_project_json"), \
         patch("services.process_manager.threading.Thread", side_effect=_inline_thread), \
         patch("services.process_manager.subprocess.run", return_value=_ok_run()), \
         patch("services.process_manager.subprocess.Popen", side_effect=fake_popen), \
         patch("services.process_manager._monitor_process"), \
         patch("services.process_manager.get_db", return_value=mock_db):
        result = process_manager.start_training(projects_dir, "myproject", branch="feature/x")

    assert "error" not in result
    mock_db.update_training_run.assert_any_call(
        2,
        persistent_dir="persistent/runs/run_2",
        tensorboard_dir="persistent/runs/run_2",
    )
    process_manager._running.clear()


def test_output_paths_link_to_persistent_run_dir_before_training_process(tmp_path):
    """Configured output paths are symlinked into persistent run storage before Popen."""
    from services import process_manager
    process_manager._running.clear()
    projects_dir = _make_project(tmp_path, output_paths=["saved_models", "exports/checkpoints"])
    workspace_dir = os.path.join(projects_dir, "myproject", "workspace")

    mock_db = MagicMock(create_training_run=MagicMock(return_value=(42, 42)),
                        delete_training_run=MagicMock(),
                        get_training_runs=MagicMock(return_value=[]))

    persistent_dir = os.path.join(projects_dir, "myproject", "persistent", "runs", "run_42")

    def fake_popen(cmd, **kwargs):
        assert os.path.islink(os.path.join(workspace_dir, "runs"))
        assert os.readlink(os.path.join(workspace_dir, "runs")) == persistent_dir
        saved_models = os.path.join(workspace_dir, "saved_models")
        checkpoints = os.path.join(workspace_dir, "exports", "checkpoints")
        assert os.path.islink(saved_models)
        assert os.readlink(saved_models) == os.path.join(persistent_dir, "saved_models")
        assert os.path.islink(checkpoints)
        assert os.readlink(checkpoints) == os.path.join(persistent_dir, "exports", "checkpoints")
        assert kwargs["env"]["BEEKEEPER_RUN_DIR"] == persistent_dir
        proc = MagicMock()
        proc.pid = 9999
        return proc

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager._resolve_tensorboard_binary", return_value=None), \
         patch("services.process_manager._update_project_json"), \
         patch("services.process_manager.threading.Thread", side_effect=_inline_thread), \
         patch("services.process_manager.subprocess.run", return_value=_ok_run()), \
         patch("services.process_manager.subprocess.Popen", side_effect=fake_popen), \
         patch("services.process_manager._monitor_process"), \
         patch("services.process_manager.get_db", return_value=mock_db):
        result = process_manager.start_training(projects_dir, "myproject")

    assert "error" not in result
    process_manager._running.clear()


def test_primary_workspace_output_conflict_warns_and_skips_symlink(tmp_path):
    """Primary workspace conflicts are non-destructive and recorded in the run log."""
    from services import process_manager
    process_manager._running.clear()
    projects_dir = _make_project(tmp_path, output_paths=["saved_models"])
    workspace_dir = os.path.join(projects_dir, "myproject", "workspace")
    conflict_dir = os.path.join(workspace_dir, "saved_models")
    os.makedirs(conflict_dir)
    with open(os.path.join(conflict_dir, "keep.txt"), "w") as f:
        f.write("do not delete")

    mock_db = MagicMock(create_training_run=MagicMock(return_value=(44, 44)),
                        delete_training_run=MagicMock(),
                        get_training_runs=MagicMock(return_value=[]))

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager._resolve_tensorboard_binary", return_value=None), \
         patch("services.process_manager._update_project_json"), \
         patch("services.process_manager.threading.Thread", side_effect=_inline_thread), \
         patch("services.process_manager.subprocess.run", return_value=_ok_run()), \
         patch("services.process_manager.subprocess.Popen") as mock_popen, \
         patch("services.process_manager._monitor_process"), \
         patch("services.process_manager.get_db", return_value=mock_db):
        mock_popen.return_value = MagicMock(pid=9999)
        result = process_manager.start_training(projects_dir, "myproject")

    assert "error" not in result
    assert os.path.isdir(conflict_dir)
    assert not os.path.islink(conflict_dir)
    with open(os.path.join(projects_dir, "myproject", "train-44.log")) as f:
        log_content = f.read()
    assert "Could not protect output path 'saved_models'" in log_content
    process_manager._running.clear()


def test_parallel_workspace_output_conflict_is_replaced(tmp_path):
    """Disposable parallel workspaces replace committed output directories with symlinks."""
    from services import process_manager
    process_manager._running.clear()
    projects_dir = _make_project(
        tmp_path,
        parallel_runs_enabled=True,
        max_parallel_runs=2,
        output_paths=["saved_models"],
    )
    parallel_ws = os.path.join(projects_dir, "myproject", "workspace-45")
    os.makedirs(os.path.join(parallel_ws, "saved_models"))
    with open(os.path.join(parallel_ws, "train.py"), "w") as f:
        f.write("print('training')")
    with open(os.path.join(parallel_ws, "saved_models", "tracked.txt"), "w") as f:
        f.write("replace me")

    mock_db = MagicMock(create_training_run=MagicMock(return_value=(45, 45)),
                        delete_training_run=MagicMock(),
                        get_training_runs=MagicMock(return_value=[]))

    process_manager._running[1] = {
        "process": MagicMock(), "starting": False,
        "project_name": "myproject", "run_pk": 1,
        "branch": "main",
        "workspace_dir": os.path.join(projects_dir, "myproject", "workspace"),
    }
    target_dir = os.path.join(projects_dir, "myproject", "persistent", "runs", "run_45", "saved_models")

    def fake_popen(cmd, **kwargs):
        link_path = os.path.join(parallel_ws, "saved_models")
        assert os.path.islink(link_path)
        assert os.readlink(link_path) == target_dir
        proc = MagicMock()
        proc.pid = 9999
        return proc

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager._resolve_tensorboard_binary", return_value=None), \
         patch("services.process_manager._update_project_json"), \
         patch("services.process_manager.threading.Thread", side_effect=_inline_thread), \
         patch("services.process_manager.subprocess.run", return_value=_ok_run()), \
         patch("services.process_manager.subprocess.Popen", side_effect=fake_popen), \
         patch("services.process_manager._monitor_process"), \
         patch("services.process_manager.get_db", return_value=mock_db):
        result = process_manager.start_training(projects_dir, "myproject")

    assert "error" not in result
    process_manager._running.clear()


def test_reserved_run_env_var_is_overridden_and_warned(tmp_path):
    """Beekeeper run env vars override project env vars and emit a run-log warning."""
    from services import process_manager
    process_manager._running.clear()
    projects_dir = _make_project(tmp_path, env_vars={"BEEKEEPER_RUN_DIR": "/tmp/wrong"})
    expected_dir = os.path.join(projects_dir, "myproject", "persistent", "runs", "run_46")

    mock_db = MagicMock(create_training_run=MagicMock(return_value=(46, 46)),
                        delete_training_run=MagicMock(),
                        get_training_runs=MagicMock(return_value=[]))

    def fake_popen(cmd, **kwargs):
        assert kwargs["env"]["BEEKEEPER_RUN_DIR"] == expected_dir
        proc = MagicMock()
        proc.pid = 9999
        return proc

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager._resolve_tensorboard_binary", return_value=None), \
         patch("services.process_manager._update_project_json"), \
         patch("services.process_manager.threading.Thread", side_effect=_inline_thread), \
         patch("services.process_manager.subprocess.run", return_value=_ok_run()), \
         patch("services.process_manager.subprocess.Popen", side_effect=fake_popen), \
         patch("services.process_manager._monitor_process"), \
         patch("services.process_manager.get_db", return_value=mock_db):
        result = process_manager.start_training(projects_dir, "myproject")

    assert "error" not in result
    with open(os.path.join(projects_dir, "myproject", "train-46.log")) as f:
        log_content = f.read()
    assert "Project env var BEEKEEPER_RUN_DIR is reserved by Beekeeper" in log_content
    process_manager._running.clear()


def test_run_history_cleanup_deletes_only_non_notable_old_run_storage(tmp_path):
    """Retention cleanup keeps notable runs and removes non-notable run storage."""
    from services import process_manager
    process_manager._running.clear()
    projects_dir = _make_project(tmp_path, run_history_max_runs=1)
    project_dir = os.path.join(projects_dir, "myproject")
    notable_dir = os.path.join(project_dir, "persistent", "runs", "run_10")
    old_dir = os.path.join(project_dir, "persistent", "runs", "run_11")
    os.makedirs(notable_dir, exist_ok=True)
    os.makedirs(old_dir, exist_ok=True)

    old_runs = [
        {"id": 46, "started_at": "2026-05-04T12:00:00", "notable": 0},
        {"id": 10, "started_at": "2026-05-03T12:00:00", "notable": 1, "persistent_dir": "persistent/runs/run_10"},
        {"id": 11, "started_at": "2026-05-02T12:00:00", "notable": 0, "persistent_dir": "persistent/runs/run_11"},
    ]
    mock_db = MagicMock(create_training_run=MagicMock(return_value=(46, 46)),
                        delete_training_run=MagicMock(),
                        get_training_runs=MagicMock(return_value=old_runs))

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager._resolve_tensorboard_binary", return_value=None), \
         patch("services.process_manager._update_project_json"), \
         patch("services.process_manager.threading.Thread", side_effect=_inline_thread), \
         patch("services.process_manager.subprocess.run", return_value=_ok_run()), \
         patch("services.process_manager.subprocess.Popen") as mock_popen, \
         patch("services.process_manager._monitor_process"), \
         patch("services.process_manager.get_db", return_value=mock_db):
        mock_popen.return_value = MagicMock(pid=9999)
        result = process_manager.start_training(projects_dir, "myproject")

    assert "error" not in result
    assert os.path.isdir(notable_dir)
    assert not os.path.exists(old_dir)
    mock_db.delete_training_run.assert_called_once_with(11)
    process_manager._running.clear()


def test_start_tensorboard_uses_persistent_runs_root(tmp_path):
    """Standalone TensorBoard points at persistent/runs for new run storage."""
    from services import process_manager
    process_manager._running.clear()
    process_manager._tb_running.clear()
    projects_dir = _make_project(tmp_path)
    tb_bin = os.path.join(projects_dir, "myproject", "venv", "bin", "tensorboard")
    os.makedirs(os.path.dirname(tb_bin), exist_ok=True)
    open(tb_bin, "w").close()

    with patch("services.process_manager._find_free_port", return_value=6006), \
         patch("services.process_manager.subprocess.Popen") as mock_popen:
        mock_popen.return_value = MagicMock()
        result = process_manager.start_tensorboard(projects_dir, "myproject")

    assert result["tb_port"] == 6006
    args = mock_popen.call_args.args[0]
    assert "--logdir" in args
    assert args[args.index("--logdir") + 1] == os.path.join(
        projects_dir, "myproject", "persistent", "runs"
    )
    process_manager._tb_running.clear()


def test_start_tensorboard_includes_nonempty_legacy_workspace_runs(tmp_path):
    """Standalone TensorBoard includes legacy workspace runs only when they exist."""
    from services import process_manager
    process_manager._running.clear()
    process_manager._tb_running.clear()
    projects_dir = _make_project(tmp_path)
    tb_bin = os.path.join(projects_dir, "myproject", "venv", "bin", "tensorboard")
    os.makedirs(os.path.dirname(tb_bin), exist_ok=True)
    open(tb_bin, "w").close()
    legacy_dir = os.path.join(projects_dir, "myproject", "workspace", "runs")
    os.makedirs(legacy_dir, exist_ok=True)
    open(os.path.join(legacy_dir, "events.out.tfevents.1"), "w").close()

    with patch("services.process_manager._find_free_port", return_value=6007), \
         patch("services.process_manager.subprocess.Popen") as mock_popen:
        mock_popen.return_value = MagicMock()
        result = process_manager.start_tensorboard(projects_dir, "myproject")

    assert result["tb_port"] == 6007
    args = mock_popen.call_args.args[0]
    assert "--logdir_spec" in args
    spec = args[args.index("--logdir_spec") + 1]
    assert f"persistent:{os.path.join(projects_dir, 'myproject', 'persistent', 'runs')}" in spec
    assert f"legacy:{legacy_dir}" in spec
    process_manager._tb_running.clear()


def test_start_training_returns_run_id(tmp_path):
    """start_training returns run_id in the response."""
    from services import process_manager
    process_manager._running.clear()
    projects_dir = _make_project(tmp_path)
    mock_db = MagicMock(create_training_run=MagicMock(return_value=(42, 42)),
                        delete_training_run=MagicMock(),
                        get_training_runs=MagicMock(return_value=[]))

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager._update_project_json"), \
         patch("services.process_manager.threading.Thread", side_effect=_inline_thread), \
         patch("services.process_manager.subprocess.run", return_value=_ok_run()), \
         patch("services.process_manager.subprocess.Popen") as mock_popen, \
         patch("services.process_manager._monitor_process"), \
         patch("services.process_manager.get_db", return_value=mock_db):
        mock_popen.return_value = MagicMock(pid=9999)
        result = process_manager.start_training(projects_dir, "myproject")

    assert result.get("run_id") == 42
    assert result.get("run_ref") == "myproject-42"


def test_sequential_primary_runs_get_distinct_log_files(tmp_path):
    """
    Two non-parallel runs that both land on the primary workspace slot must get
    their own log files. Previously both wrote to the same "train.log", so a
    second run starting before the first's monitor thread archived its log
    could truncate and overwrite it mid-archive.
    """
    from services import process_manager
    process_manager._running.clear()
    projects_dir = _make_project(tmp_path)

    mock_db = MagicMock(
        create_training_run=MagicMock(side_effect=[(50, 1), (51, 2)]),
        delete_training_run=MagicMock(),
        get_training_runs=MagicMock(return_value=[]),
    )

    def fake_popen(cmd, **kwargs):
        proc = MagicMock()
        proc.pid = 9999
        return proc

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager._resolve_tensorboard_binary", return_value=None), \
         patch("services.process_manager._update_project_json"), \
         patch("services.process_manager.threading.Thread", side_effect=_inline_thread), \
         patch("services.process_manager.subprocess.run", return_value=_ok_run()), \
         patch("services.process_manager.subprocess.Popen", side_effect=fake_popen), \
         patch("services.process_manager._monitor_process"), \
         patch("services.process_manager.get_db", return_value=mock_db):
        result1 = process_manager.start_training(projects_dir, "myproject")
        # Simulate run 50's monitor thread freeing the primary slot before
        # archiving has happened — the exact window the race lived in.
        process_manager._running.clear()
        result2 = process_manager.start_training(projects_dir, "myproject")

    assert "error" not in result1
    assert "error" not in result2

    log1 = os.path.join(projects_dir, "myproject", "train-1.log")
    log2 = os.path.join(projects_dir, "myproject", "train-2.log")
    assert os.path.isfile(log1)
    assert os.path.isfile(log2)
    process_manager._running.clear()
    process_manager._running.clear()


# --- pre-launch abort: log must remain readable afterward ---

def test_prelaunch_abort_archives_log_and_records_path(tmp_path):
    """
    A pre-launch failure (e.g. missing train_file) writes the failure message
    to the run's log file but must also archive it and record log_file_path,
    same as a real process crash does — otherwise get_run_log_path() has
    nothing to resolve once the run drops out of _running, and the failure
    reason becomes unreadable even though the file with it still exists.
    """
    from services import process_manager
    process_manager._running.clear()
    projects_dir = _make_project(tmp_path, train_file="does_not_exist.py")

    mock_db = MagicMock(
        create_training_run=MagicMock(return_value=(77, 77)),
        delete_training_run=MagicMock(),
        get_training_runs=MagicMock(return_value=[]),
        update_training_run=MagicMock(),
    )

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager.subprocess.run", return_value=_ok_run()), \
         patch("services.process_manager.subprocess.Popen") as mock_popen, \
         patch("services.process_manager._update_project_json"), \
         patch("services.process_manager.threading.Thread", side_effect=_inline_thread), \
         patch("services.process_manager.get_db", return_value=mock_db):

        process_manager.start_training(projects_dir, "myproject")

    mock_popen.assert_not_called()

    _, kwargs = mock_db.update_training_run.call_args
    assert kwargs.get("status") == "crashed"
    assert kwargs.get("log_file_path"), "pre-launch abort must archive the log, not leave log_file_path unset"

    archived_path = os.path.join(projects_dir, "myproject", kwargs["log_file_path"])
    assert os.path.isfile(archived_path)
    assert "does_not_exist.py" in open(archived_path).read()

    process_manager._running.clear()


def test_get_run_log_path_falls_back_to_live_file_when_unarchived(tmp_path):
    """
    If a run's DB record has no log_file_path (or the archive is missing),
    get_run_log_path() must fall back to the live per-run file it's known to
    write to, not the pre-per-run-log "train.log" name nothing writes anymore.
    """
    from services import process_manager
    process_manager._running.clear()
    projects_dir = str(tmp_path / "projects")
    os.makedirs(os.path.join(projects_dir, "myproject"), exist_ok=True)
    live_log = os.path.join(projects_dir, "myproject", "train-5.log")
    with open(live_log, "w") as f:
        f.write("[beekeeper] Pre-launch failed: Training file not found: train.py\n")

    mock_db = MagicMock(get_run=MagicMock(return_value={"log_file_path": None}))

    with patch("services.process_manager.get_db", return_value=mock_db):
        resolved = process_manager.get_run_log_path(projects_dir, "myproject", run_id=5)

    assert resolved == live_log


# --- GPU assignment env ---

def _launch_with_gpu_management(tmp_path, env_vars=None):
    """Run a gpu_enabled launch on a fake two-GPU host; return the env given to Popen."""
    from services import process_manager
    process_manager._running.clear()
    projects_dir = _make_project(tmp_path, gpu_enabled=True, env_vars=env_vars or {})
    gib = 1024 ** 3
    gpus = [
        {"index": 0, "name": "small", "mem_total": 12 * gib, "mem_used": 0},
        {"index": 1, "name": "big", "mem_total": 24 * gib, "mem_used": 0},
    ]
    mock_db = MagicMock(create_training_run=MagicMock(return_value=(99, 99)),
                        delete_training_run=MagicMock(),
                        get_training_runs=MagicMock(return_value=[]))

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager.subprocess.run", return_value=_ok_run()), \
         patch("services.process_manager.subprocess.Popen") as mock_popen, \
         patch("services.process_manager._update_project_json"), \
         patch("services.process_manager._monitor_process"), \
         patch("services.process_manager.get_gpu_stats", return_value=gpus), \
         patch("services.process_manager.threading.Thread", side_effect=_inline_thread), \
         patch("services.process_manager.get_db", return_value=mock_db):
        mock_popen.return_value = MagicMock(pid=9999)
        start_training(projects_dir, "myproject")
    process_manager._running.clear()
    return mock_popen.call_args.kwargs["env"]


def test_gpu_assignment_pins_cuda_device_order_to_pci_bus_id(tmp_path):
    """The assigned index comes from NVML (PCI order); CUDA must enumerate the same way."""
    env = _launch_with_gpu_management(tmp_path)

    assert env["CUDA_VISIBLE_DEVICES"] == "1"
    assert env["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID"


def test_gpu_assignment_overrides_project_cuda_device_order(tmp_path):
    env = _launch_with_gpu_management(tmp_path, env_vars={"CUDA_DEVICE_ORDER": "FASTEST_FIRST"})

    assert env["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID"


def test_refused_start_does_not_consume_a_run_number(app, ready_project, mocker):
    from services import process_manager
    from services.db_service import get_db
    projects_dir = app.config["PROJECTS_DIR"]
    mocker.patch.object(process_manager, "_resolve_python_binary", return_value="/usr/bin/python3")
    mocker.patch.object(process_manager.threading, "Thread")

    first = process_manager.start_training(projects_dir, "myproject")
    refused = process_manager.start_training(projects_dir, "myproject")  # parallel off → refused
    assert "error" in refused

    process_manager._running.clear()
    third = process_manager.start_training(projects_dir, "myproject")
    numbers = [r["run_number"] for r in get_db().get_training_runs("myproject", limit=None)]
    assert sorted(numbers) == [1, 2]


def test_concurrent_starts_leave_no_number_gap(app, ready_project, mocker):
    import threading
    from services import process_manager
    from services.db_service import get_db
    projects_dir = app.config["PROJECTS_DIR"]
    RealThread = threading.Thread  # grab before patching: the patch replaces the shared module attr
    mocker.patch.object(process_manager, "_resolve_python_binary", return_value="/usr/bin/python3")
    mocker.patch.object(process_manager.threading, "Thread")

    barrier = threading.Barrier(2)
    results = []

    def go():
        barrier.wait()
        results.append(process_manager.start_training(projects_dir, "myproject"))

    ts = [RealThread(target=go) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert sum("error" in r for r in results) == 1
    assert [r["run_number"] for r in get_db().get_training_runs("myproject", limit=None)] == [1]


def _prepare_launchable(app):
    """Give the fixture project a workspace with a train file; return (projects_dir, mocks)."""
    projects_dir = app.config["PROJECTS_DIR"]
    ws = os.path.join(projects_dir, "myproject", "workspace")
    os.makedirs(ws, exist_ok=True)
    with open(os.path.join(ws, "train.py"), "w") as f:
        f.write("print('training')")
    return projects_dir


def _launch_real_db(projects_dir, popen_side_effect=None):
    from services import process_manager
    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager._resolve_tensorboard_binary", return_value=None), \
         patch("services.process_manager._update_project_json"), \
         patch("services.process_manager.threading.Thread", side_effect=_inline_thread), \
         patch("services.process_manager.subprocess.run", return_value=_ok_run()), \
         patch("services.process_manager.subprocess.Popen",
               side_effect=popen_side_effect or (lambda cmd, **kw: MagicMock(pid=9999))) as popen, \
         patch("services.process_manager._monitor_process"):
        result = process_manager.start_training(projects_dir, "myproject")
    return result, popen


def _create_run_in_other_project():
    import datetime
    from services.db_service import get_db
    return get_db().create_training_run(
        "other", {"started_at": datetime.datetime.now(), "status": "completed"})


def test_start_training_returns_per_project_number_and_ref(app, ready_project, mocker):
    from services import process_manager
    projects_dir = app.config["PROJECTS_DIR"]
    _create_run_in_other_project()
    mocker.patch.object(process_manager, "_resolve_python_binary", return_value="/usr/bin/python3")
    mocker.patch.object(process_manager.threading, "Thread")

    result = process_manager.start_training(projects_dir, "myproject")

    assert result == {"run_id": 1, "run_ref": "myproject-1", "status": "starting"}
    assert process_manager.get_runs_for_project("myproject")[0]["run_id"] == 1
    assert "run_pk" not in process_manager.get_runs_for_project("myproject")[0]


def test_launch_uses_number_for_folder_and_env(app, ready_project):
    from services.db_service import get_db
    projects_dir = _prepare_launchable(app)
    other_pk, _ = _create_run_in_other_project()
    assert other_pk == 1

    result, popen = _launch_real_db(projects_dir)

    assert result["run_id"] == 1
    env = popen.call_args.kwargs["env"]
    assert env["BEEKEEPER_RUN_ID"] == "1"
    assert env["BEEKEEPER_RUN_REF"] == "myproject-1"
    assert env["BEEKEEPER_RUN_DIR"].endswith("persistent/runs/run_1")
    row = get_db().get_run("myproject", 1)
    assert row["persistent_dir"] == "persistent/runs/run_1"
    assert row["id"] == 2  # pk differs from the number
    assert os.path.isfile(os.path.join(projects_dir, "myproject", "train-1.log"))


def test_launch_writes_marker_with_run_pk(app, ready_project):
    from services.run_folder_migration import MARKER
    projects_dir = _prepare_launchable(app)
    _create_run_in_other_project()

    result, _ = _launch_real_db(projects_dir)

    assert "error" not in result
    marker = os.path.join(projects_dir, "myproject", "persistent", "runs", "run_1", MARKER)
    with open(marker) as f:
        assert f.read().strip() == "2"  # this run's pk, not its number


def test_launch_aborts_when_folder_belongs_to_another_run(app, ready_project):
    from services import process_manager
    from services.db_service import get_db
    from services.run_folder_migration import MARKER
    projects_dir = _prepare_launchable(app)
    folder = os.path.join(projects_dir, "myproject", "persistent", "runs", "run_1")
    os.makedirs(folder)
    with open(os.path.join(folder, MARKER), "w") as f:
        f.write("777")
    with open(os.path.join(folder, "model.pt"), "w") as f:
        f.write("precious")

    result, popen = _launch_real_db(projects_dir)

    popen.assert_not_called()
    assert get_db().get_run("myproject", 1)["status"] == "crashed"
    assert process_manager._running == {}
    assert sorted(os.listdir(folder)) == sorted([MARKER, "model.pt"])
    with open(os.path.join(folder, MARKER)) as f:
        assert f.read() == "777"
    with open(os.path.join(folder, "model.pt")) as f:
        assert f.read() == "precious"
    log_path = process_manager.get_run_log_path(projects_dir, "myproject", run_id=1)
    assert "belongs to another run" in open(log_path).read()


def test_launch_aborts_when_nonempty_folder_has_no_marker(app, ready_project):
    from services.db_service import get_db
    projects_dir = _prepare_launchable(app)
    folder = os.path.join(projects_dir, "myproject", "persistent", "runs", "run_1")
    os.makedirs(folder)
    with open(os.path.join(folder, "model.pt"), "w") as f:
        f.write("x")

    _, popen = _launch_real_db(projects_dir)

    popen.assert_not_called()
    assert get_db().get_run("myproject", 1)["status"] == "crashed"
    assert os.listdir(folder) == ["model.pt"]


def test_launch_accepts_existing_empty_or_own_marker_folder(app, ready_project):
    from services.run_folder_migration import MARKER
    projects_dir = _prepare_launchable(app)
    folder = os.path.join(projects_dir, "myproject", "persistent", "runs", "run_1")
    os.makedirs(folder)

    _, popen = _launch_real_db(projects_dir)

    popen.assert_called_once()
    assert os.path.isfile(os.path.join(folder, MARKER))


def test_stop_training_by_per_project_number(app, ready_project, mocker):
    from services import process_manager
    projects_dir = app.config["PROJECTS_DIR"]
    proc = mocker.MagicMock(pid=12345, returncode=0)
    process_manager._running[99] = {
        "process": proc, "run_pk": 99, "run_id": 3, "project_name": "myproject",
        "workspace_dir": f"{projects_dir}/myproject/workspace",
    }
    mocker.patch.object(process_manager.os, "getpgid", return_value=12345)
    mocker.patch.object(process_manager.os, "killpg")

    assert "error" in process_manager.stop_training(projects_dir, "myproject", run_id=99)
    result = process_manager.stop_training(projects_dir, "myproject", run_id=3)
    assert result["run_id"] == 3 and result["run_ref"] == "myproject-3"


def _stale_pk_row(projects_dir, persistent=True):
    """myproject run #1 with pk 2 (other project holds pk 1), as left by an unfinished migration."""
    import datetime
    from services.db_service import get_db
    _create_run_in_other_project()
    db = get_db()
    pk, n = db.create_training_run(
        "myproject", {"started_at": datetime.datetime.now(), "status": "completed"})
    assert (pk, n) == (2, 1)
    if persistent:
        db.update_training_run(pk, persistent_dir="persistent/runs/run_2",
                               tensorboard_dir="persistent/runs/run_2")
    return pk


def test_launch_aborts_when_another_row_stores_the_new_folder(app, ready_project):
    from services import process_manager
    from services.db_service import get_db
    projects_dir = _prepare_launchable(app)
    old_pk = _stale_pk_row(projects_dir)
    os.makedirs(os.path.join(projects_dir, "myproject", "persistent", "runs", "run_2"))  # empty

    result, popen = _launch_real_db(projects_dir)

    assert result["run_id"] == 2
    assert not popen.called  # avoid assert_not_called: its failure message dumps the env
    assert get_db().get_run("myproject", 2)["status"] == "crashed"
    assert get_db().get_training_run(old_pk)["persistent_dir"] == "persistent/runs/run_2"
    log_path = process_manager.get_run_log_path(projects_dir, "myproject", run_id=2)
    assert "unfinished folder migration" in open(log_path).read()


def test_launch_archives_pk_named_log_of_older_run_before_truncating(app, ready_project):
    from services import process_manager
    from services.db_service import get_db
    projects_dir = _prepare_launchable(app)
    old_pk = _stale_pk_row(projects_dir, persistent=False)
    with open(os.path.join(projects_dir, "myproject", f"train-{old_pk}.log"), "w") as f:
        f.write("old run log")

    result, popen = _launch_real_db(projects_dir)

    assert result["run_id"] == 2
    popen.assert_called_once()
    rel = get_db().get_training_run(old_pk)["log_file_path"]
    assert rel and rel.endswith("-0001.log")
    with open(process_manager.get_run_log_path(projects_dir, "myproject", run_id=1)) as f:
        assert f.read() == "old run log"

@pytest.fixture
def fake_python(monkeypatch):
    monkeypatch.setattr(pm, "_resolve_python_binary", lambda d, p: "/usr/bin/python3")


@pytest.fixture
def no_launch(monkeypatch):
    """Capture the background thread's args instead of running the pre-launch sequence."""
    captured = {}

    class FakeThread:
        def __init__(self, target, args, daemon):
            captured["args"] = args
        def start(self):
            pass

    monkeypatch.setattr(pm.threading, "Thread", FakeThread)
    return captured


def test_refusal_not_found(app):
    assert pm.start_training(app.config["PROJECTS_DIR"], "ghost")["code"] == "NOT_FOUND"


def test_refusal_setup_incomplete(app):
    make_project_dir(app, "p", setup_status="installing_deps")
    assert pm.start_training(app.config["PROJECTS_DIR"], "p")["code"] == "SETUP_INCOMPLETE"


def test_refusal_no_python(app, monkeypatch):
    make_project_dir(app, "p")
    monkeypatch.setattr(pm, "_resolve_python_binary", lambda d, p: None)
    assert pm.start_training(app.config["PROJECTS_DIR"], "p")["code"] == "NO_PYTHON"


def test_refusal_already_running(app, fake_python):
    make_project_dir(app, "p")
    pm._running[999] = {"project_name": "p", "process": None, "starting": True,
                        "run_pk": 999, "run_id": 1, "branch": "main", "workspace_dir": "x"}
    assert pm.start_training(app.config["PROJECTS_DIR"], "p")["code"] == "ALREADY_RUNNING"


def test_refusal_at_capacity(app, fake_python):
    make_project_dir(app, "p", parallel_runs_enabled=True, max_parallel_runs=1)
    pm._running[999] = {"project_name": "p", "process": None, "starting": True,
                        "run_pk": 999, "run_id": 1, "branch": "main", "workspace_dir": "x"}
    assert pm.start_training(app.config["PROJECTS_DIR"], "p")["code"] == "AT_CAPACITY"


def test_refusal_invalid_env(app, fake_python):
    make_project_dir(app, "p")
    r = pm.start_training(app.config["PROJECTS_DIR"], "p", extra_env={"A": 1})
    assert r["code"] == "INVALID_ENV"


def test_extra_env_reaches_execute_training(app, fake_python, no_launch):
    make_project_dir(app, "p")
    r = pm.start_training(app.config["PROJECTS_DIR"], "p", extra_env={"LR": "1e-5"})
    assert "error" not in r
    assert no_launch["args"][-1] == {"LR": "1e-5"}


def test_merge_run_env_precedence_and_reserved():
    merged, reserved = pm._merge_run_env({"A": "1", "B": "1"}, {"B": "2", "BEEKEEPER_RUN_ID": "x"})
    assert merged == {"A": "1", "B": "2", "BEEKEEPER_RUN_ID": "x"}
    assert reserved == ["BEEKEEPER_RUN_ID"]


def test_merge_run_env_handles_none():
    assert pm._merge_run_env(None, None) == ({}, [])


def test_is_run_live():
    pm._running[5] = {"project_name": "p"}
    assert pm.is_run_live(5) is True
    assert pm.is_run_live(6) is False


def test_api_start_error_includes_code(client, app):
    make_project_dir(app, "p", setup_status="error")
    r = client.post("/api/v1/projects/p/training/start", json={})
    assert r.status_code == 400
    assert r.get_json()["error"]["details"]["code"] == "SETUP_INCOMPLETE"
