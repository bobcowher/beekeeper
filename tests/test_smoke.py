"""
Page rendering tests — every route should return 200 with expected HTML.
Also verifies the DOM element IDs that JavaScript depends on are present.
"""
from conftest import make_project_dir

# Shared status stubs
IDLE_STATUS = {
    "status": "idle",
    "pid": None,
    "started_at": None,
    "tb_port": None,
    "elapsed": None,
}

RUNNING_STATUS = {
    "status": "running",
    "pid": 42000,
    "started_at": 1700000000.0,
    "tb_port": None,
    "elapsed": 120.0,
}

MOCK_PY_VERSIONS = [
    type("PV", (), {"version": "3.11", "source": "system"})()
]


# --- Dashboard ---

def test_dashboard_empty(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"No projects yet" in resp.data


def test_dashboard_shows_project(client, ready_project):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"myproject" in resp.data


def test_dashboard_shows_status_badge(client, ready_project):
    resp = client.get("/")
    assert b"ready" in resp.data


# --- Create project form ---

def test_create_form_loads(client, mocker):
    mocker.patch("routes.project.find_available", return_value=MOCK_PY_VERSIONS)
    mocker.patch("routes.project.has_conda", return_value=False)
    resp = client.get("/projects/new")
    assert resp.status_code == 200
    assert b'name="name"' in resp.data
    assert b'name="git_url"' in resp.data
    assert b'name="train_file"' in resp.data
    assert b'name="branch"' in resp.data


def test_create_invalid_name_redirects(client):
    resp = client.post("/projects/create", data={
        "name": "has spaces!",
        "git_url": "https://github.com/user/repo.git",
    }, follow_redirects=False)
    assert resp.status_code == 302


def test_create_duplicate_name_redirects(client, ready_project):
    resp = client.post("/projects/create", data={
        "name": "myproject",
        "git_url": "https://github.com/user/repo.git",
    }, follow_redirects=False)
    assert resp.status_code == 302


def test_create_missing_git_url_redirects(client):
    resp = client.post("/projects/create", data={
        "name": "newproject",
        "git_url": "",
    }, follow_redirects=False)
    assert resp.status_code == 302


def test_create_valid_redirects_to_detail(client, mocker):
    mocker.patch("routes.project.create_project")
    resp = client.post("/projects/create", data={
        "name": "validproject",
        "git_url": "https://github.com/user/repo.git",
        "branch": "main",
        "python_version": "3.11",
        "train_file": "train.py",
        "tensorboard_log_dir": "runs",
        "requirements_file": "requirements.txt",
        "env_type": "venv",
    }, follow_redirects=False)
    assert resp.status_code == 302
    assert b"validproject" in resp.headers.get("Location", "").encode()


# --- Project detail ---

def test_project_detail_idle(client, ready_project, mocker):
    mocker.patch("routes.project.get_training_status", return_value=IDLE_STATUS)
    resp = client.get("/projects/myproject")
    assert resp.status_code == 200
    assert b"myproject" in resp.data
    # Training controls present
    assert b"Start Training" in resp.data
    # JS config injected into page — training.js will fail silently without this
    assert b"window.TRAINING_CONFIG" in resp.data
    # DOM IDs that training.js binds event listeners to
    assert b'id="training-controls"' in resp.data
    assert b'id="btn-start"' in resp.data
    assert b'id="log-terminal"' in resp.data


def test_project_detail_running(client, ready_project, mocker):
    mocker.patch("routes.project.get_training_status", return_value=RUNNING_STATUS)
    resp = client.get("/projects/myproject")
    assert resp.status_code == 200
    assert b"Stop Training" in resp.data
    assert b'id="btn-stop"' in resp.data
    assert b"42000" in resp.data  # PID displayed


def test_project_detail_pending_setup(client, app, mocker):
    """Setup-in-progress projects should show a message, not training controls."""
    make_project_dir(app, name="pending-proj", setup_status="pending")
    mocker.patch("routes.project.get_training_status", return_value=IDLE_STATUS)
    resp = client.get("/projects/pending-proj")
    assert resp.status_code == 200
    assert b"setup must complete" in resp.data
    assert b"window.TRAINING_CONFIG" not in resp.data


def test_project_detail_not_found(client):
    resp = client.get("/projects/doesnotexist")
    assert resp.status_code == 404


# --- Edit form ---

def test_edit_form_loads(client, ready_project):
    resp = client.get("/projects/myproject/edit")
    assert resp.status_code == 200
    assert b'name="branch"' in resp.data
    assert b'name="train_file"' in resp.data
    assert b'name="tensorboard_log_dir"' in resp.data


def test_edit_not_found(client):
    resp = client.get("/projects/ghost/edit")
    assert resp.status_code == 404


# --- Delete ---

def test_delete_redirects_to_dashboard(client, ready_project, mocker):
    mocker.patch("routes.project.stop_tensorboard")
    mocker.patch("routes.project.delete_project")
    resp = client.post("/projects/myproject/delete", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"


def test_delete_not_found(client):
    resp = client.post("/projects/ghost/delete")
    assert resp.status_code == 404
