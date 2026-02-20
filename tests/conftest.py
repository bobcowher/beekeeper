import json
import os
import pytest

from app import create_app


@pytest.fixture
def app(tmp_path):
    application = create_app()
    application.config["TESTING"] = True
    application.config["PROJECTS_DIR"] = str(tmp_path / "projects")
    (tmp_path / "projects").mkdir()
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def make_project_dir(app, name="myproject", **overrides):
    """Create a project directory with project.json for testing."""
    projects_dir = app.config["PROJECTS_DIR"]
    project_dir = os.path.join(projects_dir, name)
    os.makedirs(project_dir, exist_ok=True)
    data = {
        "name": name,
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
    with open(os.path.join(project_dir, "project.json"), "w") as f:
        json.dump(data, f)
    return data


@pytest.fixture
def ready_project(app):
    return make_project_dir(app)


@pytest.fixture(autouse=True)
def reset_process_manager():
    """Ensure no process state leaks between tests."""
    from services import process_manager
    process_manager._running.clear()
    process_manager._tb_running.clear()
    yield
    process_manager._running.clear()
    process_manager._tb_running.clear()
