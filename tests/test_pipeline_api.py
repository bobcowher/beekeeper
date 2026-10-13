import time

import pytest

from services import pipeline_engine as eng
from services import pipeline_store as store
from services.db_service import get_db
from tests.conftest import make_project_dir

YAML = "name: fac\nfolder: chess\nsteps:\n  - name: a\n    type: train\n    project: p\n"


def post_yaml(client, url, text, method="post"):
    return getattr(client, method)(url, data=text, content_type="application/yaml")


def test_create_get_update_delete_yaml(client, app):
    r = post_yaml(client, "/api/v1/pipelines", YAML)
    assert r.status_code == 201
    assert r.get_json()["data"]["pipeline"]["folder"] == "chess"
    assert post_yaml(client, "/api/v1/pipelines", YAML).status_code == 409
    got = client.get("/api/v1/pipelines/fac").get_json()["data"]
    assert got["yaml"] == YAML
    r = post_yaml(client, "/api/v1/pipelines/fac", "# c\n" + YAML, method="put")
    assert r.status_code == 200
    assert client.get("/api/v1/pipelines/fac").get_json()["data"]["yaml"].startswith("# c")
    assert client.delete("/api/v1/pipelines/fac").status_code == 200
    assert client.get("/api/v1/pipelines/fac").status_code == 404


def test_create_json(client):
    r = client.post("/api/v1/pipelines", json={"name": "j", "steps": [{"name": "a", "type": "task", "command": "true"}]})
    assert r.status_code == 201
    assert "command: 'true'" in client.get("/api/v1/pipelines/j").get_json()["data"]["yaml"]


def test_invalid_pipeline_errors(client):
    r = post_yaml(client, "/api/v1/pipelines", "name: x\nsteps:\n  - {name: a, type: train, project: p, branch: no}\n")
    assert r.status_code == 400
    err = r.get_json()["error"]
    assert err["code"] == "INVALID_PIPELINE"
    assert err["details"]["errors"][0]["path"] == "steps[0].branch"


def test_put_name_mismatch_rejected(client):
    post_yaml(client, "/api/v1/pipelines", YAML)
    r = post_yaml(client, "/api/v1/pipelines/fac", YAML.replace("fac", "other"), method="put")
    assert r.status_code == 400


def test_validate_endpoint(client):
    ok = post_yaml(client, "/api/v1/pipelines/validate", YAML).get_json()["data"]
    assert ok["valid"] and "project: p" in ok["normalized_yaml"]
    bad = post_yaml(client, "/api/v1/pipelines/validate", "name: [").get_json()["data"]
    assert not bad["valid"] and bad["errors"]
    assert bad["parsed"] is None and bad["definition"] is None


def test_validate_returns_parsed_mapping_even_when_invalid(client):
    # The editor loads `parsed` into its graphical view, so it must survive validation errors.
    data = post_yaml(client, "/api/v1/pipelines/validate",
                     "name: fac\nsteps:\n  - {name: a, type: task}\n").get_json()["data"]
    assert not data["valid"]
    assert data["parsed"] == {"name": "fac", "steps": [{"name": "a", "type": "task"}]}
    assert data["definition"] is None and data["yaml"] is None    # yaml is only for JSON bodies
    ok = post_yaml(client, "/api/v1/pipelines/validate", YAML).get_json()["data"]
    assert ok["definition"]["steps"][0]["project"] == "p"


def test_validate_json_returns_yaml_for_valid_and_draft_definitions(client):
    valid = {"name": "j", "description": "d", "steps": [{"name": "a", "type": "task", "command": "echo hi\nls"}]}
    data = client.post("/api/v1/pipelines/validate", json=valid).get_json()["data"]
    assert data["valid"] and data["yaml"] == data["normalized_yaml"]
    assert "command: |-\n" in data["yaml"]          # multi-line commands stay readable
    draft = {"name": "j", "steps": [{"name": "a", "type": "train", "project": ""}]}
    data = client.post("/api/v1/pipelines/validate", json=draft).get_json()["data"]
    assert not data["valid"]
    assert "project: ''" in data["yaml"]               # half-built steps still convert


def test_list_with_folder_and_last_run(client, app):
    make_project_dir(app, "p")
    post_yaml(client, "/api/v1/pipelines", YAML)
    client.post("/api/v1/pipelines/fac/runs")
    items = client.get("/api/v1/pipelines?folder=chess").get_json()["data"]["pipelines"]
    assert items[0]["name"] == "fac" and items[0]["last_run"]["run_ref"] == "fac#1"
    assert client.get("/api/v1/pipelines?folder=other").get_json()["data"]["pipelines"] == []


def test_runs_lifecycle(client, app):
    make_project_dir(app, "p")
    post_yaml(client, "/api/v1/pipelines", YAML)
    r = client.post("/api/v1/pipelines/fac/runs")
    assert r.status_code == 201 and r.get_json()["data"]["run_ref"] == "fac#1"
    queued = client.post("/api/v1/pipelines/fac/runs")
    assert queued.status_code == 201
    assert queued.get_json()["data"] == {
        "run_id": 2, "run_ref": "fac#2", "status": "queued", "position": 1,
    }
    assert client.delete("/api/v1/pipelines/fac").status_code == 409
    one = client.get("/api/v1/pipelines/fac/runs/1").get_json()["data"]
    assert one["steps"][0]["name"] == "a" and one["overrides"] == {}
    runs = client.get("/api/v1/pipelines/fac/runs").get_json()["data"]["runs"]
    assert [(run["run_id"], run["status"]) for run in runs] == [(2, "queued"), (1, "running")]
    assert client.post("/api/v1/pipelines/fac/runs/2/cancel").get_json()["data"]["status"] == "cancelled"
    assert client.post("/api/v1/pipelines/fac/runs/1/cancel").status_code == 200
    assert client.post("/api/v1/pipelines/fac/runs/1/resume").status_code == 200


def test_start_run_accepts_branch_overrides(client, app):
    make_project_dir(app, "p")
    post_yaml(client, "/api/v1/pipelines", YAML)
    r = client.post("/api/v1/pipelines/fac/runs", json={"branches": {"a": "feat/x"}})
    assert r.status_code == 201
    run = client.get("/api/v1/pipelines/fac/runs/1").get_json()["data"]
    assert run["overrides"] == {"a": "feat/x"}
    assert run["steps"][0]["branch"] == "feat/x"


@pytest.mark.parametrize("branches", [[], {"a": 3}])
def test_start_run_rejects_invalid_branch_overrides(client, app, branches):
    make_project_dir(app, "p")
    post_yaml(client, "/api/v1/pipelines", YAML)
    r = client.post("/api/v1/pipelines/fac/runs", json={"branches": branches})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "INVALID_OVERRIDE"


def test_start_missing_project_400(client):
    post_yaml(client, "/api/v1/pipelines", YAML)
    r = client.post("/api/v1/pipelines/fac/runs")
    assert r.status_code == 400 and r.get_json()["error"]["code"] == "PROJECT_NOT_FOUND"


def test_unknown_or_unsafe_name_404(client):
    assert client.get("/api/v1/pipelines/nope").status_code == 404
    assert client.get("/api/v1/pipelines/..%2Fprojects").status_code == 404
    assert client.get("/api/v1/pipelines/Bad_Name").status_code == 404


def test_step_log(client, app):
    post_yaml(client, "/api/v1/pipelines", "name: t\nsteps:\n  - {name: s, type: task, command: 'echo hello'}\n")
    client.post("/api/v1/pipelines/t/runs")
    for _ in range(100):
        eng.tick(app.config["PROJECTS_DIR"], app.config["PIPELINES_DIR"])
        if get_db().get_pipeline_run("t", 1)["status"] != "running":
            break
        time.sleep(0.05)
    log = client.get("/api/v1/pipelines/t/runs/1/steps/s/log?tail=50").get_json()["data"]["log"]
    assert "hello" in log


def test_busy_includes_running_tasks(client, app, monkeypatch):
    monkeypatch.setattr(eng, "running_task_labels", lambda: ["t#1:s"])
    data = client.get("/api/v1/busy").get_json()["data"]
    assert data["busy"] is True and data["running_tasks"] == ["t#1:s"]


def test_project_rename_rewrites_pipeline_refs(client, app):
    make_project_dir(app, "p")
    post_yaml(client, "/api/v1/pipelines", YAML)
    r = client.post("/api/v1/projects/p/rename", json={"new_name": "p2"})
    assert r.status_code == 200
    defn, _ = store.load_pipeline(app.config["PIPELINES_DIR"], "fac")
    assert defn["steps"][0]["project"] == "p2"


def test_folder_rename_includes_pipelines(client, app):
    post_yaml(client, "/api/v1/pipelines", YAML)
    assert "chess" in client.get("/api/v1/folders").get_json()["data"]["folders"]
    client.post("/api/v1/folders/chess/rename", json={"new_name": "Chess"})
    defn, _ = store.load_pipeline(app.config["PIPELINES_DIR"], "fac")
    assert defn["folder"] == "Chess"


def test_admin_toggle_persists(client, app, monkeypatch):
    import routes.admin as admin_routes
    from services.config_service import get_config_bool, set_config
    monkeypatch.setattr(admin_routes, "save_config", lambda: None)  # never write the real config file
    old_auth = get_config_bool("auth.enabled", False)
    old_tasks = get_config_bool("pipelines.inline_tasks.enabled", True)
    try:
        client.post("/admin/settings", data={"security_section": "1", "auth_enabled": "false"})
        assert get_config_bool("pipelines.inline_tasks.enabled", True) is False
        client.post("/admin/settings", data={"security_section": "1", "auth_enabled": "false", "inline_tasks_enabled": "true"})
        assert get_config_bool("pipelines.inline_tasks.enabled", True) is True
    finally:
        set_config("pipelines.inline_tasks.enabled", "true" if old_tasks else "false")
        set_config("auth.enabled", "true" if old_auth else "false")


def test_rate_limit_form_leaves_security_toggles_alone(client, app, monkeypatch):
    import routes.admin as admin_routes
    from services.config_service import get_config_bool, get_config_int, set_config
    monkeypatch.setattr(admin_routes, "save_config", lambda: None)
    old_auth = get_config_bool("auth.enabled", False)
    old_tasks = get_config_bool("pipelines.inline_tasks.enabled", True)
    old_rate = get_config_int("api.rate_limit_per_minute", 600)
    try:
        set_config("auth.enabled", "true")
        set_config("pipelines.inline_tasks.enabled", "true")
        client.post("/admin/settings", data={"api_rate_limit": "50"})
        assert get_config_bool("auth.enabled", False) is True
        assert get_config_bool("pipelines.inline_tasks.enabled", False) is True
    finally:
        set_config("api.rate_limit_per_minute", str(old_rate))
        set_config("pipelines.inline_tasks.enabled", "true" if old_tasks else "false")
        set_config("auth.enabled", "true" if old_auth else "false")


def test_step_log_path_confined(client, app):
    outside_path = app.config["PIPELINES_DIR"] + "/../outside.log"
    marker = "outside pipeline log must stay private"
    with open(outside_path, "w") as f:
        f.write(marker)
    post_yaml(client, "/api/v1/pipelines", "name: t\nsteps:\n  - {name: s, type: task, command: 'true'}\n")
    client.post("/api/v1/pipelines/t/runs")
    db = get_db()
    run = db.get_pipeline_run("t", 1)
    sr = next(s for s in db.list_step_runs(run["id"]) if s["step_name"] == "s")
    db.update_step_run(sr["id"], log_path="../outside.log")
    r = client.get("/api/v1/pipelines/t/runs/1/steps/s/log")
    assert r.status_code == 404
    assert marker not in r.get_data(as_text=True)
