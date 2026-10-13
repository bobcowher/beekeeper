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
    assert b"Running now" in resp.data
    assert b"Recent activity" in resp.data


def test_base_layout_shows_deploy_version(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b'class="deploy-version"' in resp.data
    assert b"v1.1.0 " in resp.data


def test_projects_page_shows_project(client, ready_project):
    resp = client.get("/projects")
    assert resp.status_code == 200
    assert b"myproject" in resp.data


def test_projects_page_shows_status_badge(client, ready_project):
    resp = client.get("/projects")
    assert b"ready" in resp.data


def test_sidebar_highlights_collection_for_subpages(client, ready_project, app):
    from services import pipeline_store
    pipeline_store.save_pipeline(app.config["PIPELINES_DIR"], {
        "name": "fac", "steps": [{"name": "a", "type": "task", "command": "true"}]})
    project_html = client.get("/projects/myproject").get_data(as_text=True)
    pipeline_html = client.get("/pipelines/fac").get_data(as_text=True)
    assert 'href="/projects" class="active"' in project_html
    assert 'href="/pipelines" class="active"' in pipeline_html


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


def test_create_passes_env_vars_through(client, mocker):
    mock_create = mocker.patch("routes.project.create_project")
    resp = client.post("/projects/create", data={
        "name": "envproject",
        "git_url": "https://github.com/user/repo.git",
        "branch": "main",
        "python_version": "3.11",
        "train_file": "train.py",
        "tensorboard_log_dir": "runs",
        "requirements_file": "requirements.txt",
        "env_type": "venv",
        "env_key": ["WANDB_API_KEY", ""],
        "env_val": ["abc123", "ignored"],
    }, follow_redirects=False)
    assert resp.status_code == 302
    _, data_arg = mock_create.call_args[0]
    assert data_arg["env_vars"] == {"WANDB_API_KEY": "abc123"}


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

def test_delete_redirects_to_projects(client, ready_project, mocker):
    mocker.patch("routes.project.stop_tensorboard")
    mocker.patch("routes.project.delete_project")
    resp = client.post("/projects/myproject/delete", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/projects"


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


def test_clear_tb_logs_refuses_folder_owned_by_another_run(client, ready_project, app):
    """The link target's ownership marker must name the run whose stored folder it is."""
    import datetime
    import os
    from services.db_service import get_db
    from services.run_storage_service import MARKER
    projects_dir = app.config["PROJECTS_DIR"]
    db = get_db()
    pk, _ = db.create_training_run("myproject", {"started_at": datetime.datetime.now(),
                                                 "status": "completed", "branch": "main"})
    db.update_training_run(pk, persistent_dir="persistent/runs/run_1", tensorboard_dir="persistent/runs/run_1")
    workspace = os.path.join(projects_dir, "myproject", "workspace")
    run_dir = os.path.join(projects_dir, "myproject", "persistent", "runs", "run_1")
    os.makedirs(workspace)
    os.makedirs(run_dir)
    with open(os.path.join(run_dir, MARKER), "w") as f:
        f.write(str(pk + 100))
    open(os.path.join(run_dir, "events.out.tfevents.1"), "w").close()
    os.symlink(run_dir, os.path.join(workspace, "runs"))

    resp = client.post("/projects/myproject/clear-tb-logs", follow_redirects=False)
    assert resp.status_code == 302
    assert os.path.isfile(os.path.join(run_dir, "events.out.tfevents.1"))

    with open(os.path.join(run_dir, MARKER), "w") as f:
        f.write(str(pk))
    client.post("/projects/myproject/clear-tb-logs", follow_redirects=False)
    assert os.listdir(run_dir) == [MARKER]  # cleared, ownership marker kept


def test_clear_tb_logs_survives_marker_rewrite_failure(client, ready_project, app, mocker):
    import builtins
    import os
    from services.run_storage_service import MARKER
    projects_dir = app.config["PROJECTS_DIR"]
    workspace = os.path.join(projects_dir, "myproject", "workspace")
    run_dir = os.path.join(projects_dir, "myproject", "persistent", "runs", "run_1")
    os.makedirs(workspace)
    os.makedirs(run_dir)
    with open(os.path.join(run_dir, MARKER), "w") as f:
        f.write("1")
    os.symlink(run_dir, os.path.join(workspace, "runs"))
    real_open = builtins.open

    def failing_open(path, mode="r", *args, **kwargs):
        if str(path).endswith(MARKER) and "w" in mode:
            raise OSError(28, "No space left on device")
        return real_open(path, mode, *args, **kwargs)

    mocker.patch("builtins.open", side_effect=failing_open)
    resp = client.post("/projects/myproject/clear-tb-logs", follow_redirects=False)
    assert resp.status_code == 302


def test_clear_tb_logs_missing_directory(client, ready_project):
    resp = client.post("/projects/myproject/clear-tb-logs", follow_redirects=False)
    assert resp.status_code == 302


def test_clear_tb_logs_not_found(client):
    resp = client.post("/projects/ghost/clear-tb-logs")
    assert resp.status_code == 404


def test_app_uses_test_db_path(app, tmp_path):
    from services.db_service import get_db
    assert get_db().db_path == str(tmp_path / "beekeeper-test.db")


def test_project_page_has_queue_controls(client, ready_project):
    html = client.get("/projects/myproject").get_data(as_text=True)
    assert 'id="btn-confirm-queue"' in html
    assert 'id="queue-section"' in html


def test_pipelines_page_lists_pipeline_rows(client, app):
    from services import pipeline_store
    pipeline_store.save_pipeline(app.config["PIPELINES_DIR"], {
        "name": "fac", "folder": "chess", "steps": [{"name": "a", "type": "task", "command": "true"}]})
    html = client.get("/pipelines").get_data(as_text=True)
    assert 'data-kind="pipeline"' in html and 'href="/pipelines/fac"' in html
    assert "1 step" in html and "never run" in html
    assert "step-pill" not in html   # no progress strip until it has run
    assert 'id="pipeline-intro"' not in html


def test_pipelines_page_explains_pipelines_until_first_exists(client):
    html = client.get("/pipelines").get_data(as_text=True)
    assert 'id="pipeline-intro"' in html
    assert "Create your first pipeline" in html and 'href="/pipelines/new"' in html
    assert 'id="project-list"' not in html


def test_new_pipeline_starts_from_valid_template(client, app):
    from services import pipeline_store
    resp = client.post("/pipelines/new", data={"name": "fresh", "folder": "My Folder"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/pipelines/fresh/edit")
    defn, text = pipeline_store.load_pipeline(app.config["PIPELINES_DIR"], "fresh")
    assert defn["folder"] == "My Folder"
    assert [s["name"] for s in defn["steps"]] == ["prepare"]
    assert "$BEEKEEPER_PIPELINE_RUN_DIR" in defn["steps"][0]["command"]


def test_dashboard_activity_merges_recent_runs_and_queue(client, app):
    import datetime
    from services import pipeline_store
    from services.db_service import get_db

    db = get_db()
    training_pk, _ = db.create_training_run("myproject", {
        "started_at": datetime.datetime(2026, 10, 11, 10, 0),
        "status": "running", "branch": "main",
    })
    db.update_training_run(
        training_pk, status="completed", ended_at=datetime.datetime(2026, 10, 11, 10, 5),
        duration_seconds=300,
    )
    pipeline_store.save_pipeline(app.config["PIPELINES_DIR"], {
        "name": "fac", "steps": [{"name": "a", "type": "task", "command": "true"}]})
    pipeline_pk, _, _ = db.create_pipeline_run("fac", {
        "name": "fac", "folder": "", "description": "",
        "steps": [{"name": "a", "type": "task", "command": "true", "timeout_s": 10}],
    })
    db.update_pipeline_run(pipeline_pk, status="failed", ended_at="2026-10-11T10:06:00")
    db.enqueue_run("myproject", branch="dev")

    data = client.get("/dashboard/activity").get_json()

    assert data["queued"][0] == {
        "project": "myproject", "branch": "dev", "position": 1,
        "url": "/projects/myproject",
    }
    assert [item["run_ref"] for item in data["recent"]] == ["fac#1", "myproject-1"]
    assert [item["status"] for item in data["recent"]] == ["failed", "completed"]


def test_dashboard_recent_activity_shows_each_project_and_pipeline_once(client, app):
    import datetime
    from services.db_service import get_db

    db = get_db()
    for hour in (9, 10, 11):
        pk, _ = db.create_training_run("myproject", {
            "started_at": datetime.datetime(2026, 10, 11, hour, 0), "status": "running", "branch": "main"})
        db.update_training_run(pk, status="crashed" if hour == 11 else "completed",
                               ended_at=datetime.datetime(2026, 10, 11, hour, 5), duration_seconds=300)
    pk, _ = db.create_training_run("other", {
        "started_at": datetime.datetime(2026, 10, 11, 8, 0), "status": "running", "branch": "main"})
    db.update_training_run(pk, status="completed", ended_at=datetime.datetime(2026, 10, 11, 8, 5),
                           duration_seconds=300)
    defn = {"name": "fac", "folder": "", "description": "",
            "steps": [{"name": "a", "type": "task", "command": "true", "timeout_s": 10}]}
    for _ in range(3):
        run_pk, _, _ = db.create_pipeline_run("fac", defn)
        db.update_pipeline_run(run_pk, status="succeeded", ended_at=datetime.datetime.now().isoformat())

    recent = client.get("/dashboard/activity").get_json()["recent"]

    assert [item["run_ref"] for item in recent] == ["fac#3", "myproject-3", "other-1"]
    assert recent[1]["status"] == "crashed"


def test_dashboard_activity_reports_live_training_and_pipeline_step(client, app):
    from services import pipeline_store, process_manager
    from services.db_service import get_db

    make_project_dir(app, "trainer")
    process_manager._running[10] = {
        "project_name": "trainer", "run_pk": 10, "run_id": 4, "branch": "experiment",
        "process": None, "starting": True, "workspace_dir": "x",
    }
    definition = {
        "name": "fac", "folder": "", "description": "",
        "steps": [{"name": "deploy", "type": "task", "command": "true", "timeout_s": 10}],
    }
    pipeline_store.save_pipeline(app.config["PIPELINES_DIR"], definition)
    get_db().create_pipeline_run("fac", definition)

    running = client.get("/dashboard/activity").get_json()["running"]

    assert any(item["run_ref"] == "trainer-4" and item["branch"] == "experiment" for item in running)
    assert any(item["run_ref"] == "fac#1" and item["current_step"] == "deploy" for item in running)


def test_new_pipeline_creates_template_and_redirects(client, app):
    r = client.post("/pipelines/new", data={"name": "fresh", "folder": "x"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/pipelines/fresh/edit")
    from services import pipeline_store
    defn, _ = pipeline_store.load_pipeline(app.config["PIPELINES_DIR"], "fresh")
    assert defn["folder"] == "x" and defn["steps"][0]["type"] == "task"


def test_new_pipeline_rejects_bad_name(client):
    r = client.post("/pipelines/new", data={"name": "Bad Name"}, follow_redirects=True)
    assert r.status_code == 200
    assert '<p class="flash flash-error" role="alert">' in r.get_data(as_text=True)


def test_pipeline_page_renders(client, app):
    client.post("/pipelines/new", data={"name": "fresh"})
    html = client.get("/pipelines/fresh").get_data(as_text=True)
    for dom_id in ("step-chain", "btn-pipeline-start", "btn-pipeline-edit", "pipeline-runs"):
        assert f'id="{dom_id}"' in html
    assert 'id="pipeline-page-error" role="alert"' in html
    assert 'id="pipeline-yaml"' not in html        # editing lives on its own page
    assert client.get("/pipelines/ghost").status_code == 404


def test_pipeline_edit_page_renders_raw_yaml(client, app):
    client.post("/pipelines/new", data={"name": "fresh"})
    html = client.get("/pipelines/fresh/edit").get_data(as_text=True)
    assert 'label for="pipeline-yaml"' in html
    assert "BEEKEEPER_PIPELINE_RUN_DIR" in html             # the file's text, in the YAML tab
    assert 'id="tab-graphical"' in html and 'id="step-editor-list"' in html
    assert client.get("/pipelines/ghost/edit").status_code == 404


def test_pipeline_edit_page_opens_invalid_file(client, app):
    import os
    client.post("/pipelines/new", data={"name": "fresh"})
    path = os.path.join(app.config["PIPELINES_DIR"], "fresh", "pipeline.yaml")
    with open(path, "w") as f:
        f.write("name: fresh\nsteps: nope\n")
    html = client.get("/pipelines/fresh/edit").get_data(as_text=True)
    assert "steps: nope" in html


def test_pipelines_page_sort_key_is_epoch_seconds(client, app):
    # The list sort (dashboard.js) compares numbers; an ISO string parsed as a float is just the year.
    import re
    from services import pipeline_store
    from services.db_service import get_db
    defn = pipeline_store.save_pipeline(app.config["PIPELINES_DIR"], {
        "name": "ran", "steps": [{"name": "a", "type": "task", "command": "true"}]})
    get_db().create_pipeline_run("ran", defn)
    html = client.get("/pipelines").get_data(as_text=True)
    value = re.search(r'data-name="ran"[^>]*data-last-run="([^"]*)"', html).group(1)
    assert float(value) > 1_000_000_000
