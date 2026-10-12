import json
import os
import pytest

from app import create_app


@pytest.fixture
def app(tmp_path):
    (tmp_path / "projects").mkdir()
    (tmp_path / "pipelines").mkdir()
    application = create_app({
        "TESTING": True,
        "PROJECTS_DIR": str(tmp_path / "projects"),
        "PIPELINES_DIR": str(tmp_path / "pipelines"),
        "DB_PATH": str(tmp_path / "beekeeper-test.db"),
    })
    # The limiter is process-global (600 req/min per IP by default); keep tests independent of suite size
    from services.rate_limiter import get_rate_limiter
    get_rate_limiter()._requests.clear()
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
        "output_paths": [],
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


@pytest.fixture(autouse=True)
def reset_pipeline_tasks():
    yield
    from services import pipeline_tasks
    for pk in pipeline_tasks.running_task_pks():
        pipeline_tasks.kill_task(pk)


@pytest.fixture(autouse=True)
def reset_pipeline_engine():
    yield
    from services import pipeline_engine
    pipeline_engine._not_live_since.clear()
