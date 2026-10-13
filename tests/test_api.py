"""
JSON API endpoint tests — verify correct status codes and response shapes.
"""
import os
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


def test_training_start_rejects_invalid_branch(client, ready_project):
    r = client.post("/api/v1/projects/myproject/training/start",
                    json={"branch": "--upload-pack=evil"},
                    headers={"Authorization": "Bearer test"})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "INVALID_BRANCH"


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
    assert "gpu_platform" in body
    assert body["gpu_platform"]["platform"] in ("nvidia", "rocm", "none")


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
    db.get_run.return_value = None
    mocker.patch("services.db_service.get_db", return_value=db)

    r = client.get("/projects/myproject/history/9/log")

    assert r.status_code == 404
    assert r.get_json()["error"] == "Run not found"


def test_training_history_diff_missing_runs_use_shared_message(client, mocker):
    db = mocker.MagicMock()
    db.get_run.side_effect = [
        {"id": 1, "project_name": "myproject", "commit_sha": "a" * 40},
        None,
    ]
    mocker.patch("services.db_service.get_db", return_value=db)

    r = client.get("/projects/myproject/history/diff?from=1&to=2")

    assert r.status_code == 404
    assert r.get_json()["error"] == "Run not found"


def test_training_history_update_missing_run_uses_shared_message(client, mocker):
    db = mocker.MagicMock()
    db.get_run.return_value = None
    mocker.patch("services.db_service.get_db", return_value=db)

    r = client.patch("/projects/myproject/history/9", json={"notes": "later"})

    assert r.status_code == 404
    assert r.get_json()["error"] == "Run not found"


# --- API v1 run-scoped artifact files ---

def _seed_runs(order):
    import datetime
    from services.db_service import get_db
    db = get_db()
    out = []
    for p in order:
        pk, n = db.create_training_run(p, {"started_at": datetime.datetime.now(), "status": "completed"})
        out.append((p, pk, n))
    return out


def _make_run_dir(app, name, files=None, rel=None):
    """Seed a real run and create its stored persistent dir. Returns (run_dir, run_number)."""
    import os
    from services.db_service import get_db
    ((_, pk, n),) = _seed_runs([name])
    rel = rel or f"persistent/runs/run_{n}"
    get_db().update_training_run(pk, persistent_dir=rel)
    run_dir = os.path.join(app.config["PROJECTS_DIR"], name, rel)
    os.makedirs(run_dir, exist_ok=True)
    for rel_path, content in (files or {}).items():
        full = os.path.join(run_dir, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
    return run_dir, n


_AUTH = {"Authorization": "Bearer test"}


def test_api_v1_run_files_lists_directory(client, ready_project, app):
    _, n = _make_run_dir(app, "myproject", {"config.yaml": "lr: 0.001", "checkpoints/model.ckpt": "weights"})

    r = client.get(f"/api/v1/projects/myproject/runs/{n}/files", headers=_AUTH)

    assert r.status_code == 200
    data = r.get_json()
    assert data["success"] is True
    names = {e["name"] for e in data["data"]["entries"]}
    assert names == {"config.yaml", "checkpoints"}
    assert data["data"]["run_id"] == n
    assert data["data"]["run_ref"] == f"myproject-{n}"


def test_api_v1_run_files_downloads_file(client, ready_project, app):
    _, n = _make_run_dir(app, "myproject", {"config.yaml": "lr: 0.001"})

    r = client.get(f"/api/v1/projects/myproject/runs/{n}/files/config.yaml", headers=_AUTH)

    assert r.status_code == 200
    assert r.data == b"lr: 0.001"
    assert r.headers["Content-Disposition"].startswith("attachment")


def test_api_v1_run_files_zip_download(client, ready_project, app):
    _, n = _make_run_dir(app, "myproject", {"checkpoints/model.ckpt": "weights"})

    r = client.get(f"/api/v1/projects/myproject/runs/{n}/files/checkpoints?zip=1", headers=_AUTH)

    assert r.status_code == 200
    assert r.headers["Content-Type"] == "application/zip"


def test_api_v1_run_files_latest_resolves_to_run_with_artifacts(client, ready_project, app):
    _make_run_dir(app, "myproject", {"model.ckpt": "old"})
    _make_run_dir(app, "myproject", {"model.ckpt": "new"})

    r = client.get("/api/v1/projects/myproject/runs/latest/files/model.ckpt", headers=_AUTH)

    assert r.status_code == 200
    assert r.data == b"new"


def test_api_v1_run_files_latest_with_no_artifacts_returns_404(client, ready_project, app):
    _seed_runs(["myproject"])  # run row exists, but its persistent dir does not

    r = client.get("/api/v1/projects/myproject/runs/latest/files", headers=_AUTH)

    assert r.status_code == 404
    assert r.get_json()["error"]["code"] == "NOT_FOUND"


def test_api_v1_run_files_unknown_run_id_returns_404(client, ready_project):
    r = client.get("/api/v1/projects/myproject/runs/999/files", headers=_AUTH)

    assert r.status_code == 404
    assert r.get_json()["error"]["code"] == "NOT_FOUND"


def test_api_v1_run_files_missing_subpath_returns_404(client, ready_project, app):
    _, n = _make_run_dir(app, "myproject")

    r = client.get(f"/api/v1/projects/myproject/runs/{n}/files/nope.txt", headers=_AUTH)

    assert r.status_code == 404
    assert r.get_json()["error"]["code"] == "NOT_FOUND"


def test_api_v1_run_files_path_traversal_returns_403(client, ready_project, app):
    _, n = _make_run_dir(app, "myproject")

    r = client.get(f"/api/v1/projects/myproject/runs/{n}/files/../../../etc/passwd", headers=_AUTH)

    assert r.status_code == 403
    assert r.get_json()["error"]["code"] == "FORBIDDEN"


def test_api_v1_run_files_unknown_project_returns_404(client):
    r = client.get("/api/v1/projects/ghost/runs/5/files", headers=_AUTH)

    assert r.status_code == 404
    assert r.get_json()["error"]["code"] == "NOT_FOUND"


def test_api_run_lookup_is_per_project(client, app):
    from conftest import make_project_dir
    make_project_dir(app, name="a")
    make_project_dir(app, name="b")
    _seed_runs(["a", "b", "b"])

    ra = client.get("/api/v1/projects/a/runs/1", headers=_AUTH).get_json()["data"]["run"]
    rb = client.get("/api/v1/projects/b/runs/2", headers=_AUTH).get_json()["data"]["run"]
    assert (ra["project_name"], ra["run_id"], ra["run_ref"]) == ("a", 1, "a-1")
    assert (rb["project_name"], rb["run_id"], rb["run_ref"]) == ("b", 2, "b-2")
    assert client.get("/api/v1/projects/a/runs/2", headers=_AUTH).status_code == 404


def test_api_never_serializes_primary_key(client, app):
    from conftest import make_project_dir
    make_project_dir(app, name="a")
    _seed_runs(["a", "a"])
    runs = client.get("/api/v1/projects/a/runs", headers=_AUTH).get_json()["data"]["runs"]
    assert [r["run_id"] for r in runs] == [2, 1]
    assert all("id" not in r and "run_number" not in r for r in runs)
    assert all("id" not in r for r in client.get("/projects/a/history").get_json()["runs"])


def test_run_files_latest_uses_stored_persistent_dir(client, app):
    from conftest import make_project_dir
    make_project_dir(app, name="a")
    _, n = _make_run_dir(app, "a", {"model.pt": ""}, rel="persistent/runs/.migrating_77")

    r = client.get("/api/v1/projects/a/runs/latest/files", headers=_AUTH)
    assert r.status_code == 200
    assert "model.pt" in str(r.get_json())
    assert r.get_json()["data"]["run_id"] == n


def test_history_log_download_named_by_run_ref(client, app):
    import os
    from conftest import make_project_dir
    from services.db_service import get_db
    make_project_dir(app, name="a")
    (_, pk, n), = _seed_runs(["a"])
    os.makedirs(os.path.join(app.config["PROJECTS_DIR"], "a", "run_logs"))
    with open(os.path.join(app.config["PROJECTS_DIR"], "a", "run_logs", "x.log"), "w") as f:
        f.write("hello")
    get_db().update_training_run(pk, log_file_path="run_logs/x.log")

    r = client.get(f"/projects/a/history/{n}/log/download")
    assert r.status_code == 200
    assert "a-1.log" in r.headers["Content-Disposition"]


def _contains_value_under_key(obj, keys, value):
    if isinstance(obj, dict):
        return any(
            (k in keys and v == value) or _contains_value_under_key(v, keys, value)
            for k, v in obj.items()
        )
    if isinstance(obj, list):
        return any(_contains_value_under_key(v, keys, value) for v in obj)
    return False


def test_no_endpoint_serializes_a_primary_key(client, app, mocker):
    import os
    from conftest import make_project_dir
    from services.db_service import get_db
    from services import tensorboard_service
    make_project_dir(app, name="a")
    seeded = _seed_runs(["z"] * 40 + ["a"])           # a #1 has pk 41
    (_, pk, n), = [s for s in seeded if s[0] == "a"]
    rel = f"persistent/runs/run_{n}"
    get_db().update_training_run(pk, persistent_dir=rel, tensorboard_dir=rel)
    os.makedirs(os.path.join(app.config["PROJECTS_DIR"], "a", rel))
    mocker.patch.object(tensorboard_service, "get_metric_analysis",
                        side_effect=lambda pd, name, run_pk, *a, **k: {"metrics": {}})
    urls = [
        "/api/v1/projects/a/runs",
        f"/api/v1/projects/a/runs/{n}",
        f"/api/v1/projects/a/runs/{n}/logs",
        f"/api/v1/projects/a/runs/{n}/files",
        f"/api/v1/projects/a/runs/{n}/metrics",
        "/api/v1/projects/a/tensorboard/latest",
        f"/api/v1/projects/a/tensorboard/latest?run_id={n}",
        "/projects/a/history",
        "/projects/a/runs/",
        f"/projects/a/runs/{n}/files/",
    ]
    seen_json = 0
    for url in urls:
        r = client.get(url, headers=_AUTH)
        body = r.get_json(silent=True)
        if body is None:
            continue
        seen_json += 1
        assert not _contains_value_under_key(body, {"id", "run_id", "run_pk"}, pk), url
    assert seen_json >= 6


def test_metrics_endpoint_passes_pk_internally(client, app, mocker):
    from conftest import make_project_dir
    from services import tensorboard_service
    make_project_dir(app, name="a")
    seeded = _seed_runs(["z"] * 5 + ["a"])
    (_, pk, n), = [s for s in seeded if s[0] == "a"]
    spy = mocker.patch.object(tensorboard_service, "get_metric_analysis", return_value={"metrics": {}})

    r = client.get(f"/api/v1/projects/a/runs/{n}/metrics", headers=_AUTH)

    assert r.status_code == 200
    assert spy.call_args[0][2] == pk
    assert r.get_json()["data"]["run_id"] == n


def test_api_log_download_named_by_run_ref(client, app):
    import os
    from conftest import make_project_dir
    from services.db_service import get_db
    make_project_dir(app, name="a")
    seeded = _seed_runs(["z"] * 3 + ["a"])
    (_, pk, n), = [s for s in seeded if s[0] == "a"]
    logs = os.path.join(app.config["PROJECTS_DIR"], "a", "run_logs")
    os.makedirs(logs)
    with open(os.path.join(logs, f"run-20260101-000000-{pk:04d}.log"), "w") as f:
        f.write("x")
    get_db().update_training_run(pk, log_file_path=f"run_logs/run-20260101-000000-{pk:04d}.log")

    r = client.get(f"/api/v1/projects/a/runs/{n}/log", headers=_AUTH)
    assert "a-1.log" in r.headers["Content-Disposition"]


# --- Update project: static data directory ---

def test_api_v1_update_project_enables_data_dir(client, ready_project, app, tmp_path):
    remote = tmp_path / "system-data"
    remote.mkdir()

    r = client.patch(
        "/api/v1/projects/myproject",
        json={
            "data_dir_enabled": True,
            "data_dir_local": "data",
            "data_dir_remote": str(remote),
        },
        headers={"Authorization": "Bearer test"},
    )

    assert r.status_code == 200
    data = r.get_json()["data"]["project"]
    assert data["data_dir_enabled"] is True
    assert data["data_dir_local"] == "data"
    assert data["data_dir_remote"] == str(remote)


def test_api_v1_update_project_data_dir_requires_remote_path(client, ready_project):
    r = client.patch(
        "/api/v1/projects/myproject",
        json={"data_dir_enabled": True},
        headers={"Authorization": "Bearer test"},
    )

    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "MISSING_DATA_DIR"


def test_api_v1_update_project_data_dir_rejects_missing_path(client, ready_project):
    r = client.patch(
        "/api/v1/projects/myproject",
        json={"data_dir_enabled": True, "data_dir_remote": "/does/not/exist"},
        headers={"Authorization": "Bearer test"},
    )

    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "INVALID_DATA_DIR"


def test_api_v1_update_project_data_dir_creates_symlink_when_workspace_exists(
    client, ready_project, app, tmp_path
):
    remote = tmp_path / "system-data"
    remote.mkdir()
    workspace_dir = os.path.join(app.config["PROJECTS_DIR"], "myproject", "workspace")
    os.makedirs(workspace_dir)

    r = client.patch(
        "/api/v1/projects/myproject",
        json={
            "data_dir_enabled": True,
            "data_dir_local": "data",
            "data_dir_remote": str(remote),
        },
        headers={"Authorization": "Bearer test"},
    )

    assert r.status_code == 200
    link_path = os.path.join(workspace_dir, "data")
    assert os.path.islink(link_path)
    assert os.readlink(link_path) == str(remote)


def test_api_v1_update_project_data_dir_conflict_returns_409(
    client, ready_project, app, tmp_path
):
    remote = tmp_path / "system-data"
    remote.mkdir()
    workspace_dir = os.path.join(app.config["PROJECTS_DIR"], "myproject", "workspace")
    os.makedirs(workspace_dir)
    # Pre-existing real file (not a symlink) at the target name — conflicts.
    with open(os.path.join(workspace_dir, "data"), "w") as f:
        f.write("not a symlink")

    r = client.patch(
        "/api/v1/projects/myproject",
        json={
            "data_dir_enabled": True,
            "data_dir_local": "data",
            "data_dir_remote": str(remote),
        },
        headers={"Authorization": "Bearer test"},
    )

    assert r.status_code == 409
    assert r.get_json()["error"]["code"] == "DATA_DIR_CONFLICT"


# --- Rename project: conda env is rebuilt under the new name ---
#
# The actual rebuild mechanics (conda env remove + background _setup_project)
# are unit-tested against services.project_service.rename_project directly in
# test_project_service.py. These exercise the HTTP contract: status codes,
# response shape, and that a conda rename reports "pending" rather than
# silently claiming the project is immediately usable.

def test_api_v1_rename_conda_project_reports_pending_setup_status(
    client, app, mocker
):
    from conftest import make_project_dir
    from services import project_service

    make_project_dir(app, name="oldname", env_type="conda")

    mocker.patch.object(project_service, "_find_conda_bin", return_value=None)
    mocker.patch.object(project_service.threading, "Thread")

    r = client.post(
        "/api/v1/projects/oldname/rename",
        json={"new_name": "newname"},
        headers={"Authorization": "Bearer test"},
    )

    assert r.status_code == 200
    data = r.get_json()["data"]
    assert data == {
        "old_name": "oldname",
        "name": "newname",
        "renamed": True,
        "setup_status": "pending",
    }
    assert not os.path.isdir(os.path.join(app.config["PROJECTS_DIR"], "oldname"))
    assert os.path.isdir(os.path.join(app.config["PROJECTS_DIR"], "newname"))


def test_api_v1_rename_venv_project_does_not_touch_conda(client, app, mocker):
    from conftest import make_project_dir
    from services import project_service

    make_project_dir(app, name="oldname", env_type="venv")
    conda_spy = mocker.patch.object(project_service, "_find_conda_bin")
    thread_spy = mocker.patch.object(project_service.threading, "Thread")

    r = client.post(
        "/api/v1/projects/oldname/rename",
        json={"new_name": "newname"},
        headers={"Authorization": "Bearer test"},
    )

    assert r.status_code == 200
    assert r.get_json()["data"]["setup_status"] == "ready"
    conda_spy.assert_not_called()
    thread_spy.assert_not_called()


# --- Project delete guard ---

def _seed_starting_run():
    from services import process_manager
    process_manager._running[1] = {
        "project_name": "myproject", "run_pk": 1, "run_id": 1, "starting": True, "process": None,
    }


def test_web_delete_refused_while_run_active(client, app, ready_project):
    _seed_starting_run()
    resp = client.post("/projects/myproject/delete")
    assert resp.status_code == 302
    assert "/projects/myproject" in resp.headers["Location"]
    assert os.path.isdir(os.path.join(app.config["PROJECTS_DIR"], "myproject"))


def test_api_delete_refused_while_run_active(client, app, ready_project):
    _seed_starting_run()
    r = client.delete("/api/v1/projects/myproject", headers={"Authorization": "Bearer test"})
    assert r.status_code == 409
    assert r.get_json()["error"]["code"] == "PROJECT_BUSY"
    assert os.path.isdir(os.path.join(app.config["PROJECTS_DIR"], "myproject"))


def test_tensorboard_latest_returns_public_run_id_and_ref(client, app, mocker):
    from conftest import make_project_dir
    from services import tensorboard_service
    make_project_dir(app, name="a")
    seeded = _seed_runs(["z"] * 3 + ["a"])
    (_, pk, n), = [s for s in seeded if s[0] == "a"]
    mocker.patch.object(tensorboard_service, "get_metric_analysis", return_value={"metrics": {}})

    for url in ("/api/v1/projects/a/tensorboard/latest",
                f"/api/v1/projects/a/tensorboard/latest?run_id={n}"):
        data = client.get(url, headers=_AUTH).get_json()["data"]
        assert data["run_id"] == n
        assert data["run_ref"] == f"a-{n}"
