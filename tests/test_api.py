"""
JSON API endpoint tests — verify correct status codes and response shapes.
"""


# --- Training status ---

def test_status_returns_json_with_status_key(client, ready_project):
    resp = client.get("/projects/myproject/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "status" in data
    assert data["status"] == "idle"


def test_status_unknown_project_still_returns_idle(client):
    """status endpoint has no project-existence check — returns idle for any name."""
    resp = client.get("/projects/ghost/status")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "idle"


# --- Training start ---

def test_start_missing_project_returns_404(client):
    resp = client.post("/projects/ghost/start")
    assert resp.status_code == 404
    assert "error" in resp.get_json()


def test_start_project_not_ready_returns_400(client, app):
    """Projects with setup_status != 'ready' cannot be started."""
    from conftest import make_project_dir
    make_project_dir(app, name="pending-proj", setup_status="pending")
    resp = client.post("/projects/pending-proj/start")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_start_already_running_returns_400(client, ready_project):
    """Trying to start a project that is already in _running returns an error."""
    from unittest.mock import MagicMock
    from services import process_manager

    # Fake a running entry
    process_manager._running["myproject"] = {"process": MagicMock()}
    resp = client.post("/projects/myproject/start")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


# --- Training stop ---

def test_stop_not_running_returns_400(client, ready_project):
    resp = client.post("/projects/myproject/stop")
    assert resp.status_code == 400
    data = resp.get_json()
    assert "error" in data
    assert "not running" in data["error"].lower()


# --- Log download ---

def test_logs_download_no_log_returns_404(client, ready_project):
    resp = client.get("/projects/myproject/logs/download")
    assert resp.status_code == 404


def test_logs_download_returns_file(client, ready_project, app):
    import os
    log_path = os.path.join(app.config["PROJECTS_DIR"], "myproject", "train.log")
    with open(log_path, "w") as f:
        f.write("Epoch 1/10\nEpoch 2/10\nLoss: 0.42\n")

    resp = client.get("/projects/myproject/logs/download")
    assert resp.status_code == 200
    assert b"Epoch 1/10" in resp.data
    assert resp.headers["Content-Disposition"].startswith("attachment")


# --- System stats ---

def test_stats_returns_json(client, mocker):
    mocker.patch("routes.stats.get_all_stats", return_value={"cpu": 12.5, "ram": 64.0})
    resp = client.get("/api/stats")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["cpu"] == 12.5
    assert data["ram"] == 64.0


# --- File browser ---

def test_files_root_lists_directory(client, ready_project, app):
    import os
    src = os.path.join(app.config["PROJECTS_DIR"], "myproject", "src")
    os.makedirs(src)
    open(os.path.join(src, "train.py"), "w").close()

    resp = client.get("/projects/myproject/files/")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["project"] == "myproject"
    assert "entries" in data
    assert any(e["name"] == "train.py" for e in data["entries"])


def test_files_missing_src_returns_404(client, ready_project):
    """src/ directory does not exist — should 404."""
    resp = client.get("/projects/myproject/files/")
    assert resp.status_code == 404


def test_files_hides_pycache_and_dotfiles(client, ready_project, app):
    import os
    src = os.path.join(app.config["PROJECTS_DIR"], "myproject", "src")
    os.makedirs(os.path.join(src, "__pycache__"))
    os.makedirs(os.path.join(src, ".git"))
    open(os.path.join(src, "train.py"), "w").close()

    resp = client.get("/projects/myproject/files/")
    data = resp.get_json()
    names = [e["name"] for e in data["entries"]]
    assert "__pycache__" not in names
    assert ".git" not in names
    assert "train.py" in names
