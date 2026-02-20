"""
Tests for start_training() in process_manager.
Focuses on the pre-launch sequence: git pull → pip install → subprocess start.
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
    src_dir = proj_dir / "src"
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


def _ok_run(returncode=0, stderr=""):
    m = MagicMock()
    m.returncode = returncode
    m.stderr = stderr
    return m


# --- pip install called ---

def test_pip_install_called_with_requirements_file(tmp_path):
    """pip install must be called with the project's requirements file."""
    projects_dir = _make_project(tmp_path)

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager.subprocess.run", return_value=_ok_run()) as mock_run, \
         patch("services.process_manager.subprocess.Popen") as mock_popen, \
         patch("services.process_manager._update_project_json"), \
         patch("services.process_manager.threading.Thread"):

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
    # Remove the requirements file
    os.remove(os.path.join(projects_dir, "myproject", "src", "requirements.txt"))

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager.subprocess.run", return_value=_ok_run()) as mock_run, \
         patch("services.process_manager.subprocess.Popen") as mock_popen, \
         patch("services.process_manager._update_project_json"), \
         patch("services.process_manager.threading.Thread"):

        mock_proc = MagicMock()
        mock_proc.pid = 9999
        mock_popen.return_value = mock_proc

        start_training(projects_dir, "myproject")

    calls = [c.args[0] for c in mock_run.call_args_list]
    pip_calls = [c for c in calls if "pip" in c]
    assert len(pip_calls) == 0


# --- pip install failure ---

def test_pip_install_failure_blocks_training(tmp_path):
    """A non-zero pip exit code must return an error and not launch the process."""
    projects_dir = _make_project(tmp_path)

    def fake_run(cmd, **kwargs):
        if "pull" in cmd:
            return _ok_run()
        if "pip" in cmd:
            return _ok_run(returncode=1, stderr="ERROR: Could not find a version that satisfies the requirement fakepkg")
        return _ok_run()

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager.subprocess.run", side_effect=fake_run), \
         patch("services.process_manager.subprocess.Popen") as mock_popen:

        result = start_training(projects_dir, "myproject")

    assert "error" in result
    assert "Pip install failed" in result["error"]
    mock_popen.assert_not_called()


def test_pip_install_timeout_blocks_training(tmp_path):
    """A pip install timeout must return an error and not launch the process."""
    import subprocess as _subprocess
    projects_dir = _make_project(tmp_path)

    def fake_run(cmd, **kwargs):
        if "pull" in cmd:
            return _ok_run()
        if "pip" in cmd:
            raise _subprocess.TimeoutExpired(cmd, 300)
        return _ok_run()

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager.subprocess.run", side_effect=fake_run), \
         patch("services.process_manager.subprocess.Popen") as mock_popen:

        result = start_training(projects_dir, "myproject")

    assert "error" in result
    assert "timed out" in result["error"].lower()
    mock_popen.assert_not_called()


# --- pip runs after git pull ---

def test_pip_install_runs_after_git_pull(tmp_path):
    """pip install must happen after git pull, not before."""
    projects_dir = _make_project(tmp_path)
    call_order = []

    def fake_run(cmd, **kwargs):
        if "pull" in cmd:
            call_order.append("git_pull")
        elif "pip" in cmd:
            call_order.append("pip_install")
        return _ok_run()

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager.subprocess.run", side_effect=fake_run), \
         patch("services.process_manager.subprocess.Popen") as mock_popen, \
         patch("services.process_manager._update_project_json"), \
         patch("services.process_manager.threading.Thread"):

        mock_proc = MagicMock()
        mock_proc.pid = 9999
        mock_popen.return_value = mock_proc

        start_training(projects_dir, "myproject")

    assert call_order == ["git_pull", "pip_install"]
