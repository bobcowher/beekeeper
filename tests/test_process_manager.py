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

from services.process_manager import start_training


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
    mock_db = MagicMock(create_training_run=MagicMock(return_value=99),
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
    mock_db = MagicMock(create_training_run=MagicMock(return_value=99),
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
    mock_db = MagicMock(create_training_run=MagicMock(return_value=99),
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
    mock_db = MagicMock(create_training_run=MagicMock(return_value=99),
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
    mock_db = MagicMock(create_training_run=MagicMock(return_value=99),
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
        "run_id": 42,
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
    assert runs[0]["run_id"] == 42
    assert runs[0]["branch"] == "main"
    assert runs[0]["status"] == "running"
    process_manager._running.clear()


# --- parallel capacity ---

def test_start_training_rejects_second_run_when_parallel_disabled(tmp_path):
    """Second start_training call is rejected when parallel_runs_enabled=False."""
    from services import process_manager
    process_manager._running.clear()
    projects_dir = _make_project(tmp_path)
    mock_db = MagicMock(create_training_run=MagicMock(return_value=1),
                        delete_training_run=MagicMock(),
                        get_training_runs=MagicMock(return_value=[]))

    # Simulate run 1 already active
    process_manager._running[1] = {
        "process": MagicMock(), "starting": False,
        "project_name": "myproject", "run_id": 1,
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
    mock_db = MagicMock(create_training_run=MagicMock(return_value=2),
                        delete_training_run=MagicMock(),
                        get_training_runs=MagicMock(return_value=[]))

    # Simulate run 1 already active on primary workspace
    process_manager._running[1] = {
        "process": MagicMock(), "starting": False,
        "project_name": "myproject", "run_id": 1,
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

    mock_db = MagicMock(create_training_run=MagicMock(return_value=2),
                        delete_training_run=MagicMock(),
                        get_training_runs=MagicMock(return_value=[]))

    process_manager._running[1] = {
        "process": MagicMock(), "starting": False,
        "project_name": "myproject", "run_id": 1,
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

    mock_db = MagicMock(create_training_run=MagicMock(return_value=42),
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

    mock_db = MagicMock(create_training_run=MagicMock(return_value=44),
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

    mock_db = MagicMock(create_training_run=MagicMock(return_value=45),
                        delete_training_run=MagicMock(),
                        get_training_runs=MagicMock(return_value=[]))

    process_manager._running[1] = {
        "process": MagicMock(), "starting": False,
        "project_name": "myproject", "run_id": 1,
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

    mock_db = MagicMock(create_training_run=MagicMock(return_value=46),
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
    mock_db = MagicMock(create_training_run=MagicMock(return_value=46),
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
    mock_db = MagicMock(create_training_run=MagicMock(return_value=42),
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
        create_training_run=MagicMock(side_effect=[50, 51]),
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

    log1 = os.path.join(projects_dir, "myproject", "train-50.log")
    log2 = os.path.join(projects_dir, "myproject", "train-51.log")
    assert os.path.isfile(log1)
    assert os.path.isfile(log2)
    process_manager._running.clear()
    process_manager._running.clear()
