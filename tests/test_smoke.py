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


def test_base_layout_shows_deploy_version(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b'class="deploy-version"' in resp.data
    assert b"v1.0.9 " in resp.data


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
    # Training section present
    assert b'id="training-section"' in resp.data
    # JS config injected into page — training.js will fail silently without this
    assert b"window.TRAINING_CONFIG" in resp.data
    # DOM IDs that training.js binds event listeners to
    assert b'id="run-list"' in resp.data
    assert b'id="btn-start-run"' in resp.data


def test_project_detail_running(client, ready_project, mocker):
    mocker.patch("routes.project.get_training_status", return_value=RUNNING_STATUS)
    resp = client.get("/projects/myproject")
    assert resp.status_code == 200
    # Training section and run list are present; run rows are rendered client-side by JS
    assert b'id="training-section"' in resp.data
    assert b'id="run-list"' in resp.data


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


# --- Clear TB logs ---

def test_clear_tb_logs_real_directory(client, ready_project, app):
    import os
    tb_dir = os.path.join(app.config["PROJECTS_DIR"], "myproject", "workspace", "runs")
    os.makedirs(tb_dir)
    open(os.path.join(tb_dir, "events.out.tfevents.1"), "w").close()

    resp = client.post("/projects/myproject/clear-tb-logs", follow_redirects=False)
    assert resp.status_code == 302
    assert os.path.isdir(tb_dir)
    assert os.listdir(tb_dir) == []


def test_clear_tb_logs_symlinked_directory(client, ready_project, app):
    """workspace/<tb_dir> may be a symlink into persistent/runs/run_<id>/, set up by
    _ensure_workspace_symlink for the most recent run. shutil.rmtree refuses to operate
    on a symlink itself, so the route must clear the link target instead of crashing."""
    import os
    projects_dir = app.config["PROJECTS_DIR"]
    workspace = os.path.join(projects_dir, "myproject", "workspace")
    persistent_run_dir = os.path.join(projects_dir, "myproject", "persistent", "runs", "run_1")
    os.makedirs(workspace)
    os.makedirs(persistent_run_dir)
    open(os.path.join(persistent_run_dir, "events.out.tfevents.1"), "w").close()
    tb_link = os.path.join(workspace, "runs")
    os.symlink(persistent_run_dir, tb_link)

    resp = client.post("/projects/myproject/clear-tb-logs", follow_redirects=False)
    assert resp.status_code == 302
    assert os.path.islink(tb_link)
    assert os.path.isdir(persistent_run_dir)
    assert os.listdir(persistent_run_dir) == []


def test_clear_tb_logs_missing_directory(client, ready_project):
    resp = client.post("/projects/myproject/clear-tb-logs", follow_redirects=False)
    assert resp.status_code == 302


def test_clear_tb_logs_not_found(client):
    resp = client.post("/projects/ghost/clear-tb-logs")
    assert resp.status_code == 404
