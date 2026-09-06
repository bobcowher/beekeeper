"""
JSON API endpoint tests — verify correct status codes and response shapes.
"""
import pytest


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
    assert data["cpu"] == pytest.approx(12.5)
    assert data["ram"] == pytest.approx(64.0)


# --- File browser ---

def test_files_root_lists_directory(client, ready_project, app):
    import os
    src = os.path.join(app.config["PROJECTS_DIR"], "myproject", "workspace")
    os.makedirs(src)
    open(os.path.join(src, "train.py"), "w").close()

    resp = client.get("/projects/myproject/files/")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["project"] == "myproject"
    assert "entries" in data
    assert any(e["name"] == "train.py" for e in data["entries"])


def test_files_missing_src_returns_404(client, ready_project):
    """workspace/ directory does not exist — should 404."""
    resp = client.get("/projects/myproject/files/")
    assert resp.status_code == 404


def test_files_hides_pycache_and_dotfiles(client, ready_project, app):
    import os
    src = os.path.join(app.config["PROJECTS_DIR"], "myproject", "workspace")
    os.makedirs(os.path.join(src, "__pycache__"))
    os.makedirs(os.path.join(src, ".git"))
    open(os.path.join(src, "train.py"), "w").close()

    resp = client.get("/projects/myproject/files/")
    data = resp.get_json()
    names = [e["name"] for e in data["entries"]]
    assert "__pycache__" not in names
    assert ".git" not in names
    assert "train.py" in names


# --- API v1 training endpoints ---

def test_training_start_returns_run_id(client, ready_project):
    """POST /training/start returns run_id."""
    from unittest.mock import patch
    with patch("routes.api_v1.start_training", return_value={"run_id": 7, "status": "starting"}):
        r = client.post("/api/v1/projects/myproject/training/start",
                        json={}, headers={"Authorization": "Bearer test"})
    assert r.status_code == 200
    data = r.get_json()
    assert data["success"] is True
    assert data["data"]["run_id"] == 7


def test_training_start_passes_branch(client, ready_project):
    """POST /training/start forwards branch param."""
    from unittest.mock import patch, call
    with patch("routes.api_v1.start_training", return_value={"run_id": 8, "status": "starting"}) as mock_start:
        client.post("/api/v1/projects/myproject/training/start",
                    json={"branch": "feature/test"},
                    headers={"Authorization": "Bearer test"})
    _, kwargs = mock_start.call_args
    assert kwargs.get("branch") == "feature/test"


def test_training_status_returns_runs_list(client, ready_project):
    """GET /training/status returns runs list."""
    from unittest.mock import patch
    mock_runs = [{"run_id": 3, "branch": "main", "status": "running", "elapsed": 60, "pid": 123, "tb_port": None, "resources": None}]
    with patch("routes.api_v1.get_runs_for_project", return_value=mock_runs):
        r = client.get("/api/v1/projects/myproject/training/status",
                       headers={"Authorization": "Bearer test"})
    assert r.status_code == 200
    data = r.get_json()
    assert "runs" in data["data"]
    assert data["data"]["runs"][0]["run_id"] == 3


def test_capacity_endpoint_shape(client):
    """GET /capacity returns total_slots, running, available, projects."""
    r = client.get("/api/v1/capacity", headers={"Authorization": "Bearer test"})
    assert r.status_code == 200
    data = r.get_json()
    assert data["success"] is True
    body = data["data"]
    assert "total_slots" in body
    assert "running" in body
    assert "available" in body
    assert "projects" in body
    assert body["available"] == body["total_slots"] - body["running"]


def test_api_v1_list_projects_reads_project_file_constant(client, ready_project):
    r = client.get("/api/v1/projects", headers={"Authorization": "Bearer test"})

    assert r.status_code == 200
    data = r.get_json()
    assert data["success"] is True
    assert data["data"]["projects"][0]["name"] == "myproject"


def test_api_v1_busy_checks_project_file_constant(client, ready_project, mocker):
    mocker.patch("routes.api_v1.get_training_status", return_value={"status": "running"})

    r = client.get("/api/v1/busy", headers={"Authorization": "Bearer test"})

    assert r.status_code == 200
    data = r.get_json()["data"]
    assert data["busy"] is True
    assert data["running_projects"] == ["myproject"]
    assert data["setting_up_projects"] == []


def test_api_v1_busy_true_while_setup_in_progress(client, app, mocker):
    from conftest import make_project_dir
    make_project_dir(app, name="setting-up", setup_status="installing_deps")
    mocker.patch("routes.api_v1.get_training_status", return_value={"status": "idle"})

    r = client.get("/api/v1/busy", headers={"Authorization": "Bearer test"})

    assert r.status_code == 200
    data = r.get_json()["data"]
    assert data["busy"] is True
    assert data["running_projects"] == []
    assert data["setting_up_projects"] == ["setting-up"]


def test_api_v1_capacity_counts_ready_project(client, ready_project):
    r = client.get("/api/v1/capacity", headers={"Authorization": "Bearer test"})

    assert r.status_code == 200
    body = r.get_json()["data"]
    assert body["total_slots"] == 1
    assert body["projects"][0]["name"] == "myproject"


def test_mcp_documentation_and_download_use_mcp_server_constant(client, app, tmp_path):
    server_file = tmp_path / "mcp_server.py"
    server_file.write_text("print('beekeeper mcp')\n")
    app.config["BEEKEEPER_HOME"] = str(tmp_path)

    docs = client.get("/api/v1/mcp")
    download = client.get("/api/v1/mcp/server")

    assert docs.status_code == 200
    assert download.status_code == 200
    assert b"beekeeper mcp" in download.data


def test_training_history_log_missing_run_uses_shared_message(client, mocker):
    db = mocker.MagicMock()
    db.get_training_run.return_value = None
    mocker.patch("services.db_service.get_db", return_value=db)

    r = client.get("/projects/myproject/history/9/log")

    assert r.status_code == 404
    assert r.get_json()["error"] == "Run not found"


def test_training_history_diff_missing_runs_use_shared_message(client, mocker):
    db = mocker.MagicMock()
    db.get_training_run.side_effect = [
        {"id": 1, "project_name": "myproject", "commit_sha": "a" * 40},
        None,
    ]
    mocker.patch("services.db_service.get_db", return_value=db)

    r = client.get("/projects/myproject/history/diff?from=1&to=2")

    assert r.status_code == 404
    assert r.get_json()["error"] == "Run not found"


def test_training_history_update_missing_run_uses_shared_message(client, mocker):
    db = mocker.MagicMock()
    db.get_training_run.return_value = None
    mocker.patch("services.db_service.get_db", return_value=db)

    r = client.patch("/projects/myproject/history/9", json={"notes": "later"})

    assert r.status_code == 404
    assert r.get_json()["error"] == "Run not found"


# --- API v1 run-scoped artifact files ---

def _make_run_dir(app, name, run_id, files=None):
    """Create projects/<name>/persistent/runs/run_<id>/ with optional file contents."""
    import os
    run_dir = os.path.join(app.config["PROJECTS_DIR"], name, "persistent", "runs", f"run_{run_id}")
    os.makedirs(run_dir, exist_ok=True)
    for rel_path, content in (files or {}).items():
        full = os.path.join(run_dir, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
    return run_dir


def test_api_v1_run_files_lists_directory(client, ready_project, app):
    _make_run_dir(app, "myproject", 5, {"config.yaml": "lr: 0.001", "checkpoints/model.ckpt": "weights"})

    r = client.get("/api/v1/projects/myproject/runs/5/files",
                    headers={"Authorization": "Bearer test"})

    assert r.status_code == 200
    data = r.get_json()
    assert data["success"] is True
    names = {e["name"] for e in data["data"]["entries"]}
    assert names == {"config.yaml", "checkpoints"}


def test_api_v1_run_files_downloads_file(client, ready_project, app):
    _make_run_dir(app, "myproject", 5, {"config.yaml": "lr: 0.001"})

    r = client.get("/api/v1/projects/myproject/runs/5/files/config.yaml",
                    headers={"Authorization": "Bearer test"})

    assert r.status_code == 200
    assert r.data == b"lr: 0.001"
    assert r.headers["Content-Disposition"].startswith("attachment")


def test_api_v1_run_files_zip_download(client, ready_project, app):
    _make_run_dir(app, "myproject", 5, {"checkpoints/model.ckpt": "weights"})

    r = client.get("/api/v1/projects/myproject/runs/5/files/checkpoints?zip=1",
                    headers={"Authorization": "Bearer test"})

    assert r.status_code == 200
    assert r.headers["Content-Type"] == "application/zip"


def test_api_v1_run_files_latest_resolves_to_run_with_artifacts(client, ready_project, app, mocker):
    _make_run_dir(app, "myproject", 4, {"model.ckpt": "old"})
    _make_run_dir(app, "myproject", 6, {"model.ckpt": "new"})

    db = mocker.MagicMock()
    db.get_training_runs.return_value = [{"id": 6}, {"id": 4}]  # newest first
    mocker.patch("routes.runs.get_db", return_value=db)

    r = client.get("/api/v1/projects/myproject/runs/latest/files/model.ckpt",
                    headers={"Authorization": "Bearer test"})

    assert r.status_code == 200
    assert r.data == b"new"


def test_api_v1_run_files_latest_with_no_artifacts_returns_404(client, ready_project, app, mocker):
    db = mocker.MagicMock()
    db.get_training_runs.return_value = [{"id": 6}]
    mocker.patch("routes.runs.get_db", return_value=db)

    r = client.get("/api/v1/projects/myproject/runs/latest/files",
                    headers={"Authorization": "Bearer test"})

    assert r.status_code == 404
    assert r.get_json()["error"]["code"] == "NOT_FOUND"


def test_api_v1_run_files_unknown_run_id_returns_404(client, ready_project):
    r = client.get("/api/v1/projects/myproject/runs/999/files",
                    headers={"Authorization": "Bearer test"})

    assert r.status_code == 404
    assert r.get_json()["error"]["code"] == "NOT_FOUND"


def test_api_v1_run_files_missing_subpath_returns_404(client, ready_project, app):
    _make_run_dir(app, "myproject", 5)

    r = client.get("/api/v1/projects/myproject/runs/5/files/nope.txt",
                    headers={"Authorization": "Bearer test"})

    assert r.status_code == 404
    assert r.get_json()["error"]["code"] == "NOT_FOUND"


def test_api_v1_run_files_path_traversal_returns_403(client, ready_project, app):
    _make_run_dir(app, "myproject", 5)

    r = client.get("/api/v1/projects/myproject/runs/5/files/../../../etc/passwd",
                    headers={"Authorization": "Bearer test"})

    assert r.status_code == 403
    assert r.get_json()["error"]["code"] == "FORBIDDEN"


def test_api_v1_run_files_unknown_project_returns_404(client):
    r = client.get("/api/v1/projects/ghost/runs/5/files",
                    headers={"Authorization": "Bearer test"})

    assert r.status_code == 404
    assert r.get_json()["error"]["code"] == "NOT_FOUND"
