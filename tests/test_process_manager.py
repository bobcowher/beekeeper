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
    projects_dir = _make_project(tmp_path)

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager.subprocess.run", return_value=_ok_run()) as mock_run, \
         patch("services.process_manager.subprocess.Popen") as mock_popen, \
         patch("services.process_manager._update_project_json"), \
         patch("services.process_manager._monitor_process"), \
         patch("services.process_manager.threading.Thread", side_effect=_inline_thread):

        mock_proc = MagicMock()
        mock_proc.pid = 9999
        mock_popen.return_value = mock_proc

        start_training(projects_dir, "myproject")

    calls = [c.args[0] for c in mock_run.call_args_list]
    pip_calls = [c for c in calls if "pip" in c]
    assert len(pip_calls) == 1
    assert "-m" in pip_calls[0]
    assert "pip" in pip_calls[0]
    assert "install" in pip_calls[0]
    assert "-r" in pip_calls[0]
    assert any("requirements.txt" in arg for arg in pip_calls[0])


def test_pip_install_skipped_when_no_requirements_file(tmp_path):
    """If requirements.txt doesn't exist, pip install is silently skipped."""
    projects_dir = _make_project(tmp_path)
    os.remove(os.path.join(projects_dir, "myproject", "workspace", "requirements.txt"))

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager.subprocess.run", return_value=_ok_run()) as mock_run, \
         patch("services.process_manager.subprocess.Popen") as mock_popen, \
         patch("services.process_manager._update_project_json"), \
         patch("services.process_manager._monitor_process"), \
         patch("services.process_manager.threading.Thread", side_effect=_inline_thread):

        mock_proc = MagicMock()
        mock_proc.pid = 9999
        mock_popen.return_value = mock_proc

        start_training(projects_dir, "myproject")

    calls = [c.args[0] for c in mock_run.call_args_list]
    pip_calls = [c for c in calls if "pip" in c]
    assert len(pip_calls) == 0


# --- pip install failure ---

def test_pip_install_failure_blocks_training(tmp_path):
    """A non-zero pip exit code must not launch the process."""
    projects_dir = _make_project(tmp_path)

    def fake_run(cmd, **kwargs):
        if "pip" in cmd:
            return _ok_run(returncode=1, stderr="ERROR: Could not find a version that satisfies the requirement fakepkg")
        return _ok_run()

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager.subprocess.run", side_effect=fake_run), \
         patch("services.process_manager.subprocess.Popen") as mock_popen, \
         patch("services.process_manager._update_project_json"), \
         patch("services.process_manager.threading.Thread", side_effect=_inline_thread):

        start_training(projects_dir, "myproject")

    mock_popen.assert_not_called()


def test_pip_install_timeout_blocks_training(tmp_path):
    """A pip install timeout must not launch the process."""
    import subprocess as _subprocess
    projects_dir = _make_project(tmp_path)

    def fake_run(cmd, **kwargs):
        if "pip" in cmd:
            raise _subprocess.TimeoutExpired(cmd, 300)
        return _ok_run()

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager.subprocess.run", side_effect=fake_run), \
         patch("services.process_manager.subprocess.Popen") as mock_popen, \
         patch("services.process_manager._update_project_json"), \
         patch("services.process_manager.threading.Thread", side_effect=_inline_thread):

        start_training(projects_dir, "myproject")

    mock_popen.assert_not_called()


# --- pip runs after git sync ---

def test_pip_install_runs_after_git_pull(tmp_path):
    """pip install must happen after git sync, not before."""
    projects_dir = _make_project(tmp_path)
    call_order = []

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
         patch("services.process_manager.threading.Thread", side_effect=_inline_thread):

        mock_proc = MagicMock()
        mock_proc.pid = 9999
        mock_popen.return_value = mock_proc

        start_training(projects_dir, "myproject")

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
